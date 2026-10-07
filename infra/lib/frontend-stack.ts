import {
  CfnOutput,
  DefaultStackSynthesizer,
  Duration,
  RemovalPolicy,
  Stack,
  type StackProps,
  aws_cloudfront as cloudfront,
  aws_cloudfront_origins as origins,
  aws_iam as iam,
  aws_s3 as s3,
} from 'aws-cdk-lib';
import type { Construct } from 'constructs';

const GITHUB_OIDC_HOST = 'token.actions.githubusercontent.com';
/** GitHub environment whose required reviewer approves each infrastructure deploy. */
const GITHUB_DEPLOY_ENVIRONMENT = 'production';

export type KbFrontendStackProps = StackProps & {
  /**
   * `owner/name` of the GitHub repository whose CI may publish the site and deploy
   * the stacks.
   */
  githubRepository?: string;
  /** Stacks whose outputs the publish job reads to build the frontend. */
  outputStacks?: string[];
};

export class KbFrontendStack extends Stack {
  public readonly origin: string;

  public constructor(
    scope: Construct,
    id: string,
    props: KbFrontendStackProps = {},
  ) {
    super(scope, id, props);

    // S3-managed encryption rather than the shared KMS key: the bundle is public by
    // design, and a customer-managed key would need a CloudFront grant for no benefit.
    const siteBucket = new s3.Bucket(this, 'SiteBucket', {
      encryption: s3.BucketEncryption.S3_MANAGED,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
    });

    // The bucket stays private, so a missing key surfaces as 403 rather than 404. Both
    // map to index.html because /callback is a client-side route with no object behind it.
    const spaFallback = (httpStatus: number): cloudfront.ErrorResponse => ({
      httpStatus,
      responseHttpStatus: 200,
      responsePagePath: '/index.html',
      ttl: Duration.seconds(0),
    });
    const distribution = new cloudfront.Distribution(this, 'Distribution', {
      defaultRootObject: 'index.html',
      defaultBehavior: {
        // aws-cdk-lib 2.262.1 types Bucket.isWebsite as `boolean | undefined`, which
        // exactOptionalPropertyTypes rejects against IBucket's optional `boolean`.
        origin: origins.S3BucketOrigin.withOriginAccessControl(
          siteBucket as s3.IBucket,
        ),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        responseHeadersPolicy:
          cloudfront.ResponseHeadersPolicy.SECURITY_HEADERS,
      },
      errorResponses: [spaFallback(403), spaFallback(404)],
    });

    this.origin = `https://${distribution.distributionDomainName}`;

    new CfnOutput(this, 'FrontendUrl', { value: this.origin });
    new CfnOutput(this, 'SiteBucketName', { value: siteBucket.bucketName });
    new CfnOutput(this, 'DistributionId', {
      value: distribution.distributionId,
    });

    const repository = props.githubRepository;
    if (repository !== undefined && repository !== '') {
      // GitHub Actions exchanges a short-lived OIDC token for these roles, so the
      // repository holds no AWS keys. The subject condition is the access control.
      const github = new iam.OidcProviderNative(this, 'GithubOidc', {
        url: `https://${GITHUB_OIDC_HOST}`,
        clientIds: ['sts.amazonaws.com'],
      });
      const githubRuns = (subject: string): iam.WebIdentityPrincipal =>
        new iam.WebIdentityPrincipal(github.oidcProviderArn, {
          StringEquals: {
            [`${GITHUB_OIDC_HOST}:aud`]: 'sts.amazonaws.com',
            [`${GITHUB_OIDC_HOST}:sub`]: `repo:${repository}:${subject}`,
          },
        });
      // Only workflow runs on this repository's main branch carry this subject, which
      // excludes pull requests and forks.
      const mainBranchRuns = githubRuns('ref:refs/heads/main');
      const publishRole = new iam.Role(this, 'PublishRole', {
        roleName: 'kb-assistant-github-publish',
        assumedBy: mainBranchRuns,
      });
      publishRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['s3:ListBucket'],
          resources: [siteBucket.bucketArn],
        }),
      );
      publishRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['s3:PutObject', 's3:DeleteObject'],
          resources: [`${siteBucket.bucketArn}/*`],
        }),
      );
      publishRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['cloudfront:CreateInvalidation'],
          resources: [
            `arn:${this.partition}:cloudfront::${this.account}:distribution/${distribution.distributionId}`,
          ],
        }),
      );
      publishRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['cloudformation:DescribeStacks'],
          resources: (props.outputStacks ?? []).map(
            (name) =>
              `arn:${this.partition}:cloudformation:${this.region}:${this.account}:stack/${name}/*`,
          ),
        }),
      );
      new CfnOutput(this, 'PublishRoleArn', { value: publishRole.roleArn });

      // CI deploys through the roles `cdk bootstrap` created rather than holding
      // permissions of its own, so it can do exactly what a local `cdk deploy` can.
      const bootstrapRole = (name: string): string =>
        `arn:${this.partition}:iam::${this.account}:role/cdk-${DefaultStackSynthesizer.DEFAULT_QUALIFIER}-${name}-role-${this.account}-${this.region}`;

      // Read-only: enough for `cdk diff` to compare main against the deployed stacks.
      const planRole = new iam.Role(this, 'PlanRole', {
        roleName: 'kb-assistant-github-plan',
        assumedBy: mainBranchRuns,
      });
      planRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['sts:AssumeRole'],
          resources: [bootstrapRole('lookup')],
        }),
      );
      new CfnOutput(this, 'PlanRoleArn', { value: planRole.roleArn });

      // A job that names a GitHub environment gets the environment as its subject
      // instead of the branch, and GitHub issues that token only after the
      // environment's required reviewer approves. Trusting that subject alone is what
      // makes the approval a real gate: a run on main cannot reach this role without it.
      const deployRole = new iam.Role(this, 'DeployRole', {
        roleName: 'kb-assistant-github-deploy',
        assumedBy: githubRuns(`environment:${GITHUB_DEPLOY_ENVIRONMENT}`),
      });
      deployRole.addToPolicy(
        new iam.PolicyStatement({
          actions: ['sts:AssumeRole'],
          resources: ['deploy', 'file-publishing', 'lookup'].map(bootstrapRole),
        }),
      );
      new CfnOutput(this, 'DeployRoleArn', { value: deployRole.roleArn });
    }
  }
}
