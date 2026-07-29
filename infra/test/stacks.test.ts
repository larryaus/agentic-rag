import { App, assertions, type Environment } from 'aws-cdk-lib';
import { describe, expect, it } from 'vitest';

import { KbApiStack } from '../lib/api-stack';
import { KbAuthStack } from '../lib/auth-stack';
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
    frontendOrigin: 'http://localhost:5173',
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
    frontendOrigin: 'http://localhost:5173',
    cognitoDomainPrefix: '',
  });
  const api = new KbApiStack(app, `Api${dimension}`, {
    env,
    frontendOrigin: 'http://localhost:5173',
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
    expect(Object.values(routes)).toHaveLength(5);
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
      frontendOrigin: 'http://localhost:5173',
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

  it('never grants obsolete ingestion-job APIs', () => {
    const forbidden = new Set([
      'bedrock:StartIngestionJob',
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
        'dynamodb:Query',
        'dynamodb:UpdateItem',
        'kms:Decrypt',
        'kms:GenerateDataKey',
        's3:DeleteObject',
      ].sort(),
    );
    expect(
      applicationActions(
        actionsForRole(template, 'DocumentsFunctionServiceRole'),
      ),
    ).toEqual(
      [
        'dynamodb:GetItem',
        'dynamodb:Query',
        'kms:Decrypt',
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
});
