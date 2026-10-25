import { readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { LAMBDA_ADAPTER_LAYER_VERSION } from '../lib/python-backend';

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
  const app = new App({ context: { 'aws:cdk:bundling-stacks': [] } });
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

function assumableRoles(template: SynthTemplate, rolePrefix: string): string[] {
  const roles = template.findResources('AWS::IAM::Role');
  const roleId = Object.keys(roles).find((id) => id.startsWith(rolePrefix));
  expect(roleId).toBeDefined();
  const policies = template.findResources('AWS::IAM::Policy');
  return Object.values(policies)
    .flatMap((resource) => {
      const properties = resource.Properties as {
        Roles?: Array<{ Ref?: string }>;
        PolicyDocument?: {
          Statement?: Array<{ Action?: unknown; Resource?: unknown }>;
        };
      };
      if (!properties.Roles?.some((role) => role.Ref === roleId)) return [];
      return (properties.PolicyDocument?.Statement ?? []).flatMap(
        (statement) => {
          expect(statement.Action).toBe('sts:AssumeRole');
          return [
            ...JSON.stringify(statement.Resource).matchAll(
              /role\/([A-Za-z0-9-]+)/g,
            ),
          ].map((match) => match[1] ?? '');
        },
      );
    })
    .sort();
}

function githubTrust(subject: string): Record<string, unknown> {
  return {
    AssumeRolePolicyDocument: Match.objectLike({
      Statement: [
        Match.objectLike({
          Action: 'sts:AssumeRoleWithWebIdentity',
          Condition: {
            StringEquals: {
              'token.actions.githubusercontent.com:aud': 'sts.amazonaws.com',
              'token.actions.githubusercontent.com:sub': subject,
            },
          },
        }),
      ],
    }),
  };
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
  it('relies on the schedule to retry the reconciler instead of Lambda async retries', () => {
    const functions = primaryTemplates.api.findResources('AWS::Lambda::Function');
    const reconcilerId = Object.keys(functions).find((id) =>
      id.startsWith('ReconcilerFunction'),
    );
    expect(reconcilerId).toBeDefined();
    primaryTemplates.api.hasResourceProperties('AWS::Lambda::EventInvokeConfig', {
      FunctionName: { Ref: reconcilerId },
      MaximumRetryAttempts: 0,
    });
  });

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
    const app = new App({ context: { 'aws:cdk:bundling-stacks': [] } });
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

  it('guards the chat model with content filters and masks sensitive data in answers', () => {
    const template = primaryTemplates.api;
    const guardrails = template.findResources('AWS::Bedrock::Guardrail');
    expect(Object.keys(guardrails)).toHaveLength(1);
    const [guardrailId, guardrail] = Object.entries(guardrails)[0] ?? [];
    const properties = guardrail?.Properties as {
      ContentPolicyConfig: {
        FiltersConfig: Array<{
          Type: string;
          InputStrength: string;
          OutputStrength: string;
        }>;
      };
      SensitiveInformationPolicyConfig: {
        PiiEntitiesConfig: Array<{
          Type: string;
          Action: string;
          InputEnabled: boolean;
          OutputEnabled: boolean;
          OutputAction: string;
        }>;
      };
    };

    const filters = new Map(
      properties.ContentPolicyConfig.FiltersConfig.map((filter) => [
        filter.Type,
        [filter.InputStrength, filter.OutputStrength],
      ]),
    );
    expect(Object.fromEntries(filters)).toEqual({
      HATE: ['MEDIUM', 'MEDIUM'],
      INSULTS: ['MEDIUM', 'MEDIUM'],
      MISCONDUCT: ['MEDIUM', 'MEDIUM'],
      SEXUAL: ['MEDIUM', 'MEDIUM'],
      VIOLENCE: ['MEDIUM', 'MEDIUM'],
      // Prompt attacks are an input-only check.
      PROMPT_ATTACK: ['HIGH', 'NONE'],
    });

    const entities = properties.SensitiveInformationPolicyConfig.PiiEntitiesConfig;
    const types = entities.map((entity) => entity.Type);
    expect(types).toEqual(
      expect.arrayContaining([
        'CREDIT_DEBIT_CARD_NUMBER',
        'PASSWORD',
        'AWS_SECRET_KEY',
        'US_SOCIAL_SECURITY_NUMBER',
      ]),
    );
    // Documents legitimately hold contact details, such as a support address.
    for (const contact of ['EMAIL', 'PHONE', 'NAME', 'ADDRESS']) {
      expect(types).not.toContain(contact);
    }
    for (const entity of entities) {
      expect(entity).toEqual({
        Type: entity.Type,
        Action: 'ANONYMIZE',
        InputEnabled: false,
        OutputEnabled: true,
        OutputAction: 'ANONYMIZE',
      });
    }

    // The Lambda pins a published version, so a guardrail edit ships as a new version.
    template.hasResourceProperties('AWS::Bedrock::GuardrailVersion', {
      GuardrailIdentifier: { 'Fn::GetAtt': [guardrailId, 'GuardrailId'] },
    });
    const versionId = Object.keys(
      template.findResources('AWS::Bedrock::GuardrailVersion'),
    )[0];
    template.hasResourceProperties('AWS::Lambda::Function', {
      Environment: {
        Variables: Match.objectLike({
          GUARDRAIL_ID: { 'Fn::GetAtt': [guardrailId, 'GuardrailId'] },
          GUARDRAIL_VERSION: { 'Fn::GetAtt': [versionId, 'Version'] },
        }),
      },
    });

    expect(actionsForRole(template, 'ChatFunctionServiceRole')).toContain(
      'bedrock:ApplyGuardrail',
    );
    const applyStatements = statements(template).filter(
      (statement) => statement.Action === 'bedrock:ApplyGuardrail',
    );
    expect(applyStatements).toHaveLength(1);
    expect(applyStatements[0]?.Resource).toEqual({
      'Fn::GetAtt': [guardrailId, 'GuardrailArn'],
    });
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
      new KbFrontendStack(new App({ context: { 'aws:cdk:bundling-stacks': [] } }), 'Frontend', { env }),
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
    const app = new App({ context: { 'aws:cdk:bundling-stacks': [] } });
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
    const app = new App({ context: { 'aws:cdk:bundling-stacks': [] } });
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
      new KbBudgetStack(new App({ context: { 'aws:cdk:bundling-stacks': [] } }), 'Budget', {
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
      new KbFrontendStack(new App({ context: { 'aws:cdk:bundling-stacks': [] } }), 'FrontendCi', {
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

  it('lets main-branch runs read the deployed stacks to plan a deploy, and nothing more', () => {
    const template = Template.fromStack(
      new KbFrontendStack(new App({ context: { 'aws:cdk:bundling-stacks': [] } }), 'FrontendPlan', {
        env,
        githubRepository: 'octo/kb',
        outputStacks: ['KbFrontendStack'],
      }),
    );
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'kb-assistant-github-plan',
      ...githubTrust('repo:octo/kb:ref:refs/heads/main'),
    });
    expect(assumableRoles(template, 'PlanRole')).toEqual([
      'cdk-hnb659fds-lookup-role-123456789012-us-east-1',
    ]);
  });

  it('lets only runs approved into the production environment deploy the stacks', () => {
    const template = Template.fromStack(
      new KbFrontendStack(new App({ context: { 'aws:cdk:bundling-stacks': [] } }), 'FrontendDeploy', {
        env,
        githubRepository: 'octo/kb',
        outputStacks: ['KbFrontendStack'],
      }),
    );
    // The environment subject replaces the branch subject, so a run on main cannot
    // take this role until the environment's reviewer has approved it.
    template.hasResourceProperties('AWS::IAM::Role', {
      RoleName: 'kb-assistant-github-deploy',
      ...githubTrust('repo:octo/kb:environment:production'),
    });
    expect(assumableRoles(template, 'DeployRole')).toEqual([
      'cdk-hnb659fds-deploy-role-123456789012-us-east-1',
      'cdk-hnb659fds-file-publishing-role-123456789012-us-east-1',
      'cdk-hnb659fds-lookup-role-123456789012-us-east-1',
    ]);
  });

  it('creates no CI access unless a repository is configured', () => {
    const template = Template.fromStack(
      new KbFrontendStack(new App({ context: { 'aws:cdk:bundling-stacks': [] } }), 'FrontendNoCi', { env }),
    );
    template.resourceCountIs('AWS::IAM::OIDCProvider', 0);
    expect(
      Object.keys(template.findResources('AWS::IAM::Role')).filter((id) =>
        /^(Publish|Plan|Deploy)Role/.test(id),
      ),
    ).toHaveLength(0);
  });
});


it('preserves every authored IAM statement from base 307ef1a', () => {
  const template = primaryTemplates.api;
  const roles = template.findResources('AWS::IAM::Role');
  const policies = template.findResources('AWS::IAM::Policy');
  const snapshot: Record<string, unknown> = {};
  for (const name of ['Chat', 'Presign', 'Documents', 'Sessions', 'Ingest', 'Reconciler']) {
    const id = Object.keys(roles).find((id) => id.startsWith(`${name}FunctionServiceRole`));
    expect(id).toBeDefined();
    snapshot[name] = Object.values(policies).flatMap((resource) => {
      const props = resource.Properties as { Roles?: { Ref?: string }[]; PolicyDocument: { Statement: unknown[] } };
      return props.Roles?.some((role) => role.Ref === id) ? props.PolicyDocument.Statement : [];
    });
  }
  expect(snapshot).toEqual(JSON.parse(readFileSync(path.join(__dirname, 'fixtures', 'iam-base-307ef1a.json'), 'utf8')));
});

it('uses one Python ARM64 asset and the adapter only for HTTP functions', () => {
  const functions = primaryTemplates.api.findResources('AWS::Lambda::Function');
  const code = new Set<string>();
  for (const name of ['Chat', 'Presign', 'Documents', 'Sessions', 'Ingest', 'Reconciler']) {
    const resource = Object.entries(functions).find(([id]) => id.startsWith(`${name}Function`))?.[1];
    expect(resource).toBeDefined();
    const props = resource!.Properties as { Runtime: string; Handler: string; Architectures: string[]; Code: unknown; Layers?: unknown[]; Environment: { Variables: Record<string, unknown> } };
    code.add(JSON.stringify(props.Code));
    expect(props.Runtime).toBe('python3.12');
    expect(props.Architectures).toEqual(['arm64']);
    const variables = props.Environment.Variables;
    expect(variables.DEPLOY_TARGET).toBe('aws');
    expect(variables).not.toHaveProperty('CORS_ALLOW_ORIGINS');
    expect(variables).not.toHaveProperty('NODE_OPTIONS');
    if (['Ingest', 'Reconciler'].includes(name)) {
      expect(props.Handler).toBe(`kb_assistant.adapters.aws.lambda_events.${name.toLowerCase()}_handler`);
      expect(props.Layers).toBeUndefined();
      expect(variables).not.toHaveProperty('AWS_LAMBDA_EXEC_WRAPPER');
    } else {
      expect(props.Handler).toBe('run.sh');
      expect(props.Layers).toEqual([`arn:aws:lambda:us-east-1:753240598075:layer:LambdaAdapterLayerArm64:${LAMBDA_ADAPTER_LAYER_VERSION}`]);
      const routes: Record<string, string> = { Chat: 'chat', Presign: 'uploads', Documents: 'documents', Sessions: 'sessions' };
      expect(variables).toMatchObject({ AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap', AWS_LWA_PORT: '8080', AWS_LWA_READINESS_CHECK_PATH: '/healthz', OIDC_REQUIRED_SCOPE: 'kb-api/access', KB_ROUTES: routes[name] });
      expect(variables.OIDC_ISSUER).toBeDefined();
      expect(variables.OIDC_JWKS_URL).toBeDefined();
      expect(variables.OIDC_CLIENT_ID).toBeDefined();
      expect(variables.AWS_LWA_INVOKE_MODE).toBe(name === 'Chat' ? 'response_stream' : undefined);
    }
  }
  expect(code.size).toBe(1);
});

// The backend boots each function with exactly this environment (backend/tests/
// test_function_env.py). Regenerate with UPDATE_FUNCTION_ENV=1 after an intended change.
describe('function environments shared with the backend tests', () => {
  it('match the checked-in fixture', () => {
    const template = Template.fromStack(stacks().api);
    const functions = template.findResources('AWS::Lambda::Function');
    const actual: Record<string, { handler: string; environment: Record<string, string | null> }> = {};
    for (const [logicalId, resource] of Object.entries(functions)) {
      const match = /^(Chat|Ingest|Reconciler|Presign|Documents|Sessions)Function[0-9A-F]{8}$/.exec(logicalId);
      if (match === null) continue;
      const properties = resource.Properties as {
        Handler: string;
        Environment: { Variables: Record<string, unknown> };
      };
      // Literal values are kept; deploy-time references become null placeholders.
      const environment = Object.fromEntries(
        Object.entries(properties.Environment.Variables)
          .sort(([left], [right]) => left.localeCompare(right))
          .map(([name, value]) => [name, typeof value === 'string' ? value : null]),
      );
      actual[`${match[1]}Function`] = { handler: properties.Handler, environment };
    }
    const fixture = path.join(__dirname, '..', '..', 'backend', 'tests', 'fixtures', 'function-env.json');
    const serialized = `${JSON.stringify(actual, null, 2)}\n`;
    if (process.env.UPDATE_FUNCTION_ENV === '1') {
      writeFileSync(fixture, serialized);
    }
    expect(Object.keys(actual).sort()).toEqual([
      'ChatFunction', 'DocumentsFunction', 'IngestFunction', 'PresignFunction', 'ReconcilerFunction', 'SessionsFunction',
    ]);
    expect(JSON.parse(readFileSync(fixture, 'utf8'))).toEqual(actual);
  });
});
