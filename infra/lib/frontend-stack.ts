import {
  CfnOutput,
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

export type KbFrontendStackProps = StackProps & {
  /** `owner/name` of the GitHub repository whose CI may publish the site. */
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

    if (props.githubRepository !== undefined && props.githubRepository !== '') {
      // GitHub Actions exchanges a short-lived OIDC token for this role, so the
      // repository holds no AWS keys. The subject condition is the access control:
      // only workflow runs on this repository's main branch match it, which excludes
      // pull requests and forks.
      const github = new iam.OidcProviderNative(this, 'GithubOidc', {
        url: `https://${GITHUB_OIDC_HOST}`,
        clientIds: ['sts.amazonaws.com'],
      });
      const publishRole = new iam.Role(this, 'PublishRole', {
        roleName: 'kb-assistant-github-publish',
        assumedBy: new iam.WebIdentityPrincipal(github.oidcProviderArn, {
          StringEquals: {
            [`${GITHUB_OIDC_HOST}:aud`]: 'sts.amazonaws.com',
            [`${GITHUB_OIDC_HOST}:sub`]: `repo:${props.githubRepository}:ref:refs/heads/main`,
          },
        }),
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
    }
  }
}
