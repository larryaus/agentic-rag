import { App, assertions, type Environment } from 'aws-cdk-lib';
import { describe, expect, it } from 'vitest';

import { KbApiStack } from '../lib/api-stack';
import { KbAuthStack } from '../lib/auth-stack';
import { KbBudgetStack } from '../lib/budget-stack';
import { KbFrontendStack } from '../lib/frontend-stack';
import { KbKnowledgeBaseStack } from '../lib/knowledge-base-stack';
import { KbStorageStack } from '../lib/storage-stack';

const { Match, Template } = assertions;
type SynthTemplate = ReturnType<typeof Template.fromStack>;
const env: Environment = { account: '123456789012', region: 'us-east-1' };

function stacks(dimension = 1024): {
  storage: KbStorageStack;
  knowledgeBase: KbKnowledgeBaseStack;
  auth: KbAuthStack;
  api: KbApiStack;
} {
  const app = new App();
  const storage = new KbStorageStack(app, `Storage${dimension}`, {
    env,
    frontendOrigins: ['http://localhost:5173'],
    embeddingDimension: dimension,
  });
  const knowledgeBase = new KbKnowledgeBaseStack(
    app,
    `KnowledgeBase${dimension}`,
    {
      env,
      documentsBucket: storage.documentsBucket,
      dataKey: storage.dataKey,
      vectorBucketArn: storage.vectorBucketArn,
      vectorIndexArn: storage.vectorIndexArn,
      embeddingModelId: 'amazon.titan-embed-text-v2:0',
      embeddingDimension: dimension,
    },
  );
  const auth = new KbAuthStack(app, `Auth${dimension}`, {
    env,
    frontendOrigins: ['http://localhost:5173'],
    cognitoDomainPrefix: '',
  });
  const api = new KbApiStack(app, `Api${dimension}`, {
    env,
    frontendOrigins: ['http://localhost:5173'],
    chatModelId: 'us.anthropic.test-model-v1:0',
    documentsBucket: storage.documentsBucket,
    conversationsTable: storage.conversationsTable,
    dataKey: storage.dataKey,
    knowledgeBaseId: knowledgeBase.knowledgeBaseId,
    dataSourceId: knowledgeBase.dataSourceId,
    userPool: auth.userPool,
    userPoolClient: auth.userPoolClient,
  });
  return { storage, knowledgeBase, auth, api };
}

function statements(template: SynthTemplate): Array<Record<string, unknown>> {
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.values(policies).flatMap((resource) => {
    const properties = resource.Properties as {
      PolicyDocument?: { Statement?: Array<Record<string, unknown>> };
    };
    return properties.PolicyDocument?.Statement ?? [];
  });
}

function actionsForRole(
  template: SynthTemplate,
  rolePrefix: string,
): Set<string> {
  const roles = template.findResources('AWS::IAM::Role');
  const roleId = Object.keys(roles).find((id) => id.startsWith(rolePrefix));
  expect(roleId).toBeDefined();
  const policies = template.findResources('AWS::IAM::Policy');
  const actions = new Set<string>();
  for (const resource of Object.values(policies)) {
    const properties = resource.Properties as {
      Roles?: Array<{ Ref?: string }>;
      PolicyDocument?: {
        Statement?: Array<{ Action?: string | string[] }>;
      };
    };
    if (!properties.Roles?.some((role) => role.Ref === roleId)) continue;
    for (const statement of properties.PolicyDocument?.Statement ?? []) {
      const values = Array.isArray(statement.Action)
        ? statement.Action
        : statement.Action === undefined
          ? []
          : [statement.Action];
      values.forEach((action) => actions.add(action));
    }
  }
  return actions;
}

function applicationActions(actions: Set<string>): string[] {
  return [...actions]
    .filter(
      (action) => !action.startsWith('logs:') && !action.startsWith('xray:'),
    )
    .sort();
}

const primaryStacks = stacks();
const primaryTemplates = {
  storage: Template.fromStack(primaryStacks.storage),
  knowledgeBase: Template.fromStack(primaryStacks.knowledgeBase),
  auth: Template.fromStack(primaryStacks.auth),
  api: Template.fromStack(primaryStacks.api),
};

describe('CDK stacks', () => {
  it('synthesizes storage security, PITR, TTL, EventBridge, and vectors', () => {
    const template = primaryTemplates.storage;
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
      BucketEncryption: Match.objectLike({
        ServerSideEncryptionConfiguration: Match.anyValue(),
      }),
      NotificationConfiguration: {
        EventBridgeConfiguration: { EventBridgeEnabled: true },
      },
    });
    template.hasResourceProperties('AWS::DynamoDB::Table', {
      PointInTimeRecoverySpecification: {
        PointInTimeRecoveryEnabled: true,
      },
      TimeToLiveSpecification: {
        AttributeName: 'ttl',
        Enabled: true,
      },
      GlobalSecondaryIndexes: Match.arrayWith([
        Match.objectLike({ IndexName: 'gsi1' }),
      ]),
    });
    template.hasResourceProperties('AWS::S3Vectors::Index', {
      Dimension: 1024,
      DistanceMetric: 'cosine',
      // Without these, chunks over 2 KB cannot be stored and ingestion fails.
      MetadataConfiguration: {
        NonFilterableMetadataKeys: [
          'AMAZON_BEDROCK_TEXT',
          'AMAZON_BEDROCK_METADATA',
        ],
      },
    });
  });

  it('synthesizes named S3 Vectors knowledge-base resources', () => {
    const template = primaryTemplates.knowledgeBase;
    template.hasResourceProperties('AWS::Bedrock::KnowledgeBase', {
      Name: Match.stringLikeRegexp('.+'),
      StorageConfiguration: Match.objectLike({ Type: 'S3_VECTORS' }),
    });
    template.hasResourceProperties('AWS::Bedrock::DataSource', {
      Name: Match.stringLikeRegexp('.+'),
      KnowledgeBaseId: Match.anyValue(),
    });
  });

  it('declares the department schema name without a token prefix', () => {
    primaryTemplates.auth.hasResourceProperties('AWS::Cognito::UserPool', {
      Schema: Match.arrayWith([
        Match.objectLike({ Name: 'department', AttributeDataType: 'String' }),
      ]),
    });
  });

  it('configures streaming, concurrency, schedule, scopes, and safe environments', () => {
    const template = primaryTemplates.api;
    template.hasResourceProperties('AWS::Lambda::Url', {
      InvokeMode: 'RESPONSE_STREAM',
    });
    template.hasResourceProperties('AWS::Lambda::Function', {
      ReservedConcurrentExecutions: 5,
    });
    template.hasResourceProperties('AWS::Events::Rule', {
      ScheduleExpression: 'rate(1 minute)',
    });

    const functions = template.findResources('AWS::Lambda::Function');
    for (const resource of Object.values(functions)) {
      const variables = (
        resource.Properties as {
          Environment?: { Variables?: Record<string, unknown> };
        }
      ).Environment?.Variables;
      expect(variables).not.toHaveProperty('AWS_REGION');
    }
    const routes = template.findResources('AWS::ApiGatewayV2::Route');
    expect(Object.values(routes)).toHaveLength(6);
    expect(
      Object.values(routes).map(
        (resource) => (resource.Properties as { RouteKey: string }).RouteKey,
      ),
    ).toContain('DELETE /v1/documents/{documentId}');
    // A browser preflights DELETE, so the route is unreachable without this.
    template.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      CorsConfiguration: Match.objectLike({
        AllowMethods: Match.arrayWith(['DELETE']),
      }),
    });
    Object.values(routes).forEach((resource) => {
      expect(resource.Properties).toEqual(
        expect.objectContaining({
          AuthorizationScopes: ['kb-api/access'],
        }),
      );
    });
  });

  it('threads a 512 dimension into both index and embedding configuration', () => {
    const app = new App();
    const storage = new KbStorageStack(app, 'Storage512Only', {
      env,
      frontendOrigins: ['http://localhost:5173'],
      embeddingDimension: 512,
    });
    const knowledgeBase = new KbKnowledgeBaseStack(app, 'KnowledgeBase512Only', {
      env,
      documentsBucket: storage.documentsBucket,
      dataKey: storage.dataKey,
      vectorBucketArn: storage.vectorBucketArn,
      vectorIndexArn: storage.vectorIndexArn,
      embeddingModelId: 'amazon.titan-embed-text-v2:0',
      embeddingDimension: 512,
    });
    Template.fromStack(storage).hasResourceProperties(
      'AWS::S3Vectors::Index',
      { Dimension: 512 },
    );
    Template.fromStack(knowledgeBase).hasResourceProperties(
      'AWS::Bedrock::KnowledgeBase',
      {
        KnowledgeBaseConfiguration: {
          Type: 'VECTOR',
          VectorKnowledgeBaseConfiguration: Match.objectLike({
            EmbeddingModelConfiguration: {
              BedrockEmbeddingModelConfiguration: Match.objectLike({
                Dimensions: 512,
              }),
            },
          }),
        },
      },
    );
  });

  it('never grants ingestion-job read APIs', () => {
    // StartIngestionJob is absent from this list on purpose: Bedrock requires it for
    // IngestKnowledgeBaseDocuments, and the exact-actions test pins where it is granted.
    const forbidden = new Set([
      'bedrock:GetIngestionJob',
      'bedrock:ListIngestionJobs',
    ]);
    for (const template of Object.values(primaryTemplates)) {
      for (const statement of statements(template)) {
        const raw = statement.Action;
        const actions = Array.isArray(raw) ? raw : [raw];
        expect(actions.some((action) => forbidden.has(String(action)))).toBe(
          false,
        );
      }
    }
  });

  it('never grants the nonexistent DynamoDB TransactWriteItems action', () => {
    for (const template of Object.values(primaryTemplates)) {
      for (const statement of statements(template)) {
        const raw = statement.Action;
        const actions = Array.isArray(raw) ? raw : [raw];
        expect(actions).not.toContain('dynamodb:TransactWriteItems');
      }
    }
  });

  it('grants the exact Stage 1 actions to data-plane Lambda roles', () => {
    const template = primaryTemplates.api;
    expect(
      applicationActions(actionsForRole(template, 'PresignFunctionServiceRole')),
    ).toEqual(
      [
        'dynamodb:PutItem',
        'kms:Decrypt',
        'kms:GenerateDataKey',
        's3:PutObject',
      ].sort(),
    );
    expect(
      applicationActions(actionsForRole(template, 'IngestFunctionServiceRole')),
    ).toEqual(
      [
        'bedrock:IngestKnowledgeBaseDocuments',
        'bedrock:StartIngestionJob',
        'dynamodb:GetItem',
        'dynamodb:UpdateItem',
        'kms:Decrypt',
        'kms:GenerateDataKey',
        's3:DeleteObject',
      ].sort(),
    );
    expect(
      applicationActions(
        actionsForRole(template, 'ReconcilerFunctionServiceRole'),
      ),
    ).toEqual(
      [
        'bedrock:GetKnowledgeBaseDocuments',
        'bedrock:IngestKnowledgeBaseDocuments',
        'bedrock:StartIngestionJob',
        'dynamodb:Query',
        'dynamodb:UpdateItem',
        'kms:Decrypt',
        'kms:GenerateDataKey',
        's3:DeleteObject',
        's3:GetObject',
        's3:ListBucket',
      ].sort(),
    );
    expect(
      applicationActions(
        actionsForRole(template, 'DocumentsFunctionServiceRole'),
      ),
    ).toEqual(
      [
        'dynamodb:DeleteItem',
        'dynamodb:GetItem',
        'dynamodb:Query',
        'kms:Decrypt',
        's3:DeleteObject',
        's3:GetObject',
      ].sort(),
    );
  });

  it('has no wildcard resources in authored Lambda policies except X-Ray', () => {
    const template = primaryTemplates.api;
    const rolePrefixes = [
      'ChatFunctionServiceRole',
      'IngestFunctionServiceRole',
      'ReconcilerFunctionServiceRole',
      'PresignFunctionServiceRole',
      'DocumentsFunctionServiceRole',
      'SessionsFunctionServiceRole',
    ];
    const roles = template.findResources('AWS::IAM::Role');
    const roleIds = new Set(
      Object.keys(roles).filter((id) =>
        rolePrefixes.some((prefix) => id.startsWith(prefix)),
      ),
    );
    expect(roleIds.size).toBe(6);

    const policies = template.findResources('AWS::IAM::Policy');
    for (const resource of Object.values(policies)) {
      const properties = resource.Properties as {
        Roles?: Array<{ Ref?: string }>;
        PolicyDocument?: {
          Statement?: Array<{
            Action?: string | string[];
            Resource?: unknown;
          }>;
        };
      };
      if (!properties.Roles?.some((role) => roleIds.has(role.Ref ?? ''))) {
        continue;
      }
      for (const statement of properties.PolicyDocument?.Statement ?? []) {
        const resources = Array.isArray(statement.Resource)
          ? statement.Resource
          : [statement.Resource];
        if (!resources.includes('*')) continue;
        const actions = Array.isArray(statement.Action)
          ? statement.Action
          : [statement.Action];
        expect(
          actions.every(
            (action) =>
              action === 'xray:PutTraceSegments' ||
              action === 'xray:PutTelemetryRecords',
          ),
        ).toBe(true);
      }
    }
  });

  it('serves the frontend from a private bucket behind CloudFront', () => {
    const template = Template.fromStack(
      new KbFrontendStack(new App(), 'Frontend', { env }),
    );
    template.hasResourceProperties('AWS::S3::Bucket', {
      PublicAccessBlockConfiguration: {
        BlockPublicAcls: true,
        BlockPublicPolicy: true,
        IgnorePublicAcls: true,
        RestrictPublicBuckets: true,
      },
    });
    template.resourceCountIs('AWS::CloudFront::OriginAccessControl', 1);
    template.hasResourceProperties('AWS::CloudFront::Distribution', {
      DistributionConfig: Match.objectLike({
        DefaultRootObject: 'index.html',
        DefaultCacheBehavior: Match.objectLike({
          ViewerProtocolPolicy: 'redirect-to-https',
        }),
        // /callback has no object behind it; a private bucket answers 403, not 404.
        CustomErrorResponses: [403, 404].map((ErrorCode) =>
          Match.objectLike({
            ErrorCode,
            ResponseCode: 200,
            ResponsePagePath: '/index.html',
          }),
        ),
      }),
    });
  });

  it('allows every frontend origin for sign-in and CORS', () => {
    const app = new App();
    const frontendOrigins = [
      'http://localhost:5173',
      'https://d111111abcdef8.cloudfront.net',
    ];
    const storage = new KbStorageStack(app, 'StorageOrigins', {
      env,
      frontendOrigins,
      embeddingDimension: 1024,
    });
    const auth = new KbAuthStack(app, 'AuthOrigins', {
      env,
      frontendOrigins,
      cognitoDomainPrefix: '',
    });
    const api = new KbApiStack(app, 'ApiOrigins', {
      env,
      frontendOrigins,
      chatModelId: 'au.anthropic.claude-sonnet-4-6',
      documentsBucket: storage.documentsBucket,
      conversationsTable: storage.conversationsTable,
      dataKey: storage.dataKey,
      knowledgeBaseId: 'KB12345678',
      dataSourceId: 'DS12345678',
      userPool: auth.userPool,
      userPoolClient: auth.userPoolClient,
    });

    Template.fromStack(auth).hasResourceProperties(
      'AWS::Cognito::UserPoolClient',
      {
        CallbackURLs: frontendOrigins.map((origin) => `${origin}/callback`),
        LogoutURLs: frontendOrigins,
      },
    );
    Template.fromStack(storage).hasResourceProperties('AWS::S3::Bucket', {
      CorsConfiguration: {
        CorsRules: [Match.objectLike({ AllowedOrigins: frontendOrigins })],
      },
    });
    const apiTemplate = Template.fromStack(api);
    apiTemplate.hasResourceProperties('AWS::ApiGatewayV2::Api', {
      CorsConfiguration: Match.objectLike({ AllowOrigins: frontendOrigins }),
    });
    apiTemplate.hasResourceProperties('AWS::Lambda::Url', {
      Cors: Match.objectLike({ AllowOrigins: frontendOrigins }),
    });
  });

  it('grants the chat role both the regional profile and its foundation model', () => {
    const app = new App();
    const storage = new KbStorageStack(app, 'StorageModel', {
      env,
      frontendOrigins: ['http://localhost:5173'],
      embeddingDimension: 1024,
    });
    const auth = new KbAuthStack(app, 'AuthModel', {
      env,
      frontendOrigins: ['http://localhost:5173'],
      cognitoDomainPrefix: '',
    });
    const api = new KbApiStack(app, 'ApiModel', {
      env,
      frontendOrigins: ['http://localhost:5173'],
      chatModelId: 'au.anthropic.claude-sonnet-4-6',
      documentsBucket: storage.documentsBucket,
      conversationsTable: storage.conversationsTable,
      dataKey: storage.dataKey,
      knowledgeBaseId: 'KB12345678',
      dataSourceId: 'DS12345678',
      userPool: auth.userPool,
      userPoolClient: auth.userPoolClient,
    });
    const rendered = JSON.stringify(statements(Template.fromStack(api)));
    expect(rendered).toContain(
      ':inference-profile/au.anthropic.claude-sonnet-4-6',
    );
    expect(rendered).toContain(
      '::foundation-model/anthropic.claude-sonnet-4-6',
    );
  });

  it('emails the owner as account spend approaches and passes the monthly budget', () => {
    const template = Template.fromStack(
      new KbBudgetStack(new App(), 'Budget', {
        env,
        alertEmail: 'owner@example.com',
        monthlyLimitUsd: 10,
      }),
    );
    const subscribers = [
      { SubscriptionType: 'EMAIL', Address: 'owner@example.com' },
    ];
    const alert = (NotificationType: string, Threshold: number) => ({
      Notification: {
        NotificationType,
        ComparisonOperator: 'GREATER_THAN',
        Threshold,
        ThresholdType: 'PERCENTAGE',
      },
      Subscribers: subscribers,
    });
    template.hasResourceProperties('AWS::Budgets::Budget', {
      Budget: Match.objectLike({
        BudgetType: 'COST',
        TimeUnit: 'MONTHLY',
        BudgetLimit: { Amount: 10, Unit: 'USD' },
      }),
      NotificationsWithSubscribers: [
        alert('ACTUAL', 80),
        alert('ACTUAL', 100),
        alert('FORECASTED', 100),
      ],
    });
  });

  it('lets only main-branch GitHub Actions runs of the repository publish the frontend', () => {
    const template = Template.fromStack(
      new KbFrontendStack(new App(), 'FrontendCi', {
        env,
        githubRepository: 'octo/kb',
        outputStacks: ['KbAuthStack', 'KbApiStack', 'KbFrontendStack'],
      }),
    );
    template.hasResourceProperties('AWS::IAM::OIDCProvider', {
      Url: 'https://token.actions.githubusercontent.com',
      ClientIdList: ['sts.amazonaws.com'],
    });
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'kb-assistant-github-publish',
      AssumeRolePolicyDocument: Match.objectLike({
        Statement: [
          Match.objectLike({
            Action: 'sts:AssumeRoleWithWebIdentity',
            Condition: {
              StringEquals: {
                'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
                'token.actions.githubusercontent.com:sub':
                  'repo:octo/kb:ref:refs/heads/main',
              },
            },
          }),
        ],
      }),
    });
    expect([...actionsForRole(template, 'PublishRole')].sort()).toEqual([
      'cloudformation:DescribeStacks',
      'cloudfront:CreateInvalidation',
      's3:DeleteObject',
      's3:ListBucket',
      's3:PutObject',
    ]);
    for (const statement of statements(template)) {
      expect(JSON.stringify(statement.Resource)).not.toBe('"*"');
    }
  });

  it('creates no CI access unless a repository is configured', () => {
    const template = Template.fromStack(
      new KbFrontendStack(new App(), 'FrontendNoCi', { env }),
    );
    template.resourceCountIs('AWS::IAM::OIDCProvider', 0);
    expect(
      Object.keys(template.findResources('AWS::IAM::Role')).filter((id) =>
        id.startsWith('PublishRole'),
      ),
    ).toHaveLength(0);
  });
});
