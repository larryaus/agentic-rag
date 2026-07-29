import {
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  type StackProps,
  aws_cognito as cognito,
} from 'aws-cdk-lib';
import type { Construct } from 'constructs';

export type KbAuthStackProps = StackProps & {
  frontendOrigin: string;
  cognitoDomainPrefix: string;
};

export class KbAuthStack extends Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolClient: cognito.UserPoolClient;
  public readonly domainPrefix: string;

  public constructor(
    scope: Construct,
    id: string,
    props: KbAuthStackProps,
  ) {
    super(scope, id, props);

    this.userPool = new cognito.UserPool(this, 'KbUserPool', {
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      standardAttributes: {
        email: { required: true, mutable: false },
      },
      passwordPolicy: { minLength: 12 },
      mfa: cognito.Mfa.OPTIONAL,
      mfaSecondFactor: { otp: true, sms: false },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      removalPolicy: RemovalPolicy.DESTROY,
      customAttributes: {
        // Custom attributes cannot be removed or have their type changed once defined, so
        // declare it now; Stage 2 metadata filtering depends on it.
        department: new cognito.StringAttribute({
          mutable: true,
          minLen: 1,
          maxLen: 64,
        }),
      },
    });

    const accessScope = new cognito.ResourceServerScope({
      scopeName: 'access',
      scopeDescription: 'Access the enterprise knowledge base API',
    });
    const resourceServer = this.userPool.addResourceServer(
      'KbApiResourceServer',
      {
        identifier: 'kb-api',
        scopes: [accessScope],
      },
    );

    this.userPoolClient = this.userPool.addClient('WebClient', {
      generateSecret: false,
      authFlows: { userSrp: true },
      oAuth: {
        flows: { authorizationCodeGrant: true },
        scopes: [
          cognito.OAuthScope.OPENID,
          cognito.OAuthScope.EMAIL,
          cognito.OAuthScope.PROFILE,
          cognito.OAuthScope.resourceServer(resourceServer, accessScope),
        ],
        callbackUrls: [`${props.frontendOrigin}/callback`],
        logoutUrls: [props.frontendOrigin],
      },
      accessTokenValidity: Duration.hours(1),
      idTokenValidity: Duration.hours(1),
      refreshTokenValidity: Duration.days(30),
      preventUserExistenceErrors: true,
    });

    this.domainPrefix =
      props.cognitoDomainPrefix.trim() === ''
        ? `kb-assistant-${this.account}`
        : props.cognitoDomainPrefix;
    this.userPool.addDomain('UserPoolDomain', {
      cognitoDomain: { domainPrefix: this.domainPrefix },
    });

    new CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
    });
    new CfnOutput(this, 'UserPoolClientId', {
      value: this.userPoolClient.userPoolClientId,
    });
    new CfnOutput(this, 'CognitoDomainPrefix', {
      value: this.domainPrefix,
    });
    new CfnOutput(this, 'Region', { value: this.region });
  }
}
