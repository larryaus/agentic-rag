import path from 'node:path';

import {
  CfnOutput,
  Duration,
  RemovalPolicy,
  Stack,
  type StackProps,
  aws_apigatewayv2 as apigatewayv2,
  aws_cloudwatch as cloudwatch,
  aws_events as events,
  aws_events_targets as targets,
  aws_iam as iam,
  aws_lambda as lambda,
  aws_lambda_nodejs as nodejs,
  aws_logs as logs,
} from 'aws-cdk-lib';
import type {
  aws_cognito as cognito,
  aws_dynamodb as dynamodb,
  aws_kms as kms,
  aws_s3 as s3,
} from 'aws-cdk-lib';
import { HttpUserPoolAuthorizer } from 'aws-cdk-lib/aws-apigatewayv2-authorizers';
import { HttpLambdaIntegration } from 'aws-cdk-lib/aws-apigatewayv2-integrations';
import type { Construct } from 'constructs';

export type KbApiStackProps = StackProps & {
  frontendOrigin: string;
  chatModelId: string;
  documentsBucket: s3.Bucket;
  conversationsTable: dynamodb.Table;
  dataKey: kms.IKey;
  knowledgeBaseId: string;
  dataSourceId: string;
  userPool: cognito.IUserPool;
  userPoolClient: cognito.IUserPoolClient;
};

type FunctionOptions = {
  id: string;
  handler: string;
  memorySize: number;
  timeout: Duration;
  environment: Record<string, string>;
  reservedConcurrentExecutions?: number;
};

export class KbApiStack extends Stack {
  public constructor(
    scope: Construct,
    id: string,
    props: KbApiStackProps,
  ) {
    super(scope, id, props);

    const createFunction = (options: FunctionOptions): nodejs.NodejsFunction => {
      const logGroup = new logs.LogGroup(this, `${options.id}LogGroup`, {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      return new nodejs.NodejsFunction(this, options.id, {
        entry: path.join(
          __dirname,
          '..',
          '..',
          'backend',
          'src',
          'handlers',
          options.handler,
        ),
        runtime: lambda.Runtime.NODEJS_22_X,
        architecture: lambda.Architecture.ARM_64,
        tracing: lambda.Tracing.ACTIVE,
        logGroup,
        memorySize: options.memorySize,
        timeout: options.timeout,
        environment: {
          ...options.environment,
          NODE_OPTIONS: '--enable-source-maps',
        },
        ...(options.reservedConcurrentExecutions === undefined
          ? {}
          : {
              reservedConcurrentExecutions:
                options.reservedConcurrentExecutions,
            }),
        bundling: {
          minify: true,
          sourceMap: true,
          format: nodejs.OutputFormat.ESM,
        },
      });
    };

    const chat = createFunction({
      id: 'ChatFunction',
      handler: 'chat.ts',
      memorySize: 1024,
      timeout: Duration.minutes(5),
      reservedConcurrentExecutions: 5,
      environment: {
        TABLE_NAME: props.conversationsTable.tableName,
        KNOWLEDGE_BASE_ID: props.knowledgeBaseId,
        CHAT_MODEL_ID: props.chatModelId,
        USER_POOL_ID: props.userPool.userPoolId,
        USER_POOL_CLIENT_ID: props.userPoolClient.userPoolClientId,
        RETRIEVAL_TOP_K: '8',
        MAX_TOOL_ITERATIONS: '6',
        MAX_HISTORY_MESSAGES: '20',
        SESSION_TTL_DAYS: '90',
      },
    });
    const ingest = createFunction({
      id: 'IngestFunction',
      handler: 'ingest.ts',
      memorySize: 512,
      timeout: Duration.minutes(1),
      environment: {
        TABLE_NAME: props.conversationsTable.tableName,
        KNOWLEDGE_BASE_ID: props.knowledgeBaseId,
        DATA_SOURCE_ID: props.dataSourceId,
        DOCS_BUCKET: props.documentsBucket.bucketName,
        MAX_UPLOAD_BYTES: '26214400',
      },
    });
    const reconciler = createFunction({
      id: 'ReconcilerFunction',
      handler: 'reconciler.ts',
      memorySize: 512,
      timeout: Duration.minutes(2),
      reservedConcurrentExecutions: 1,
      environment: {
        TABLE_NAME: props.conversationsTable.tableName,
        KNOWLEDGE_BASE_ID: props.knowledgeBaseId,
        DATA_SOURCE_ID: props.dataSourceId,
        DOCS_BUCKET: props.documentsBucket.bucketName,
        ABANDONED_UPLOAD_MINUTES: '10',
      },
    });
    const presign = createFunction({
      id: 'PresignFunction',
      handler: 'presign.ts',
      memorySize: 256,
      timeout: Duration.seconds(15),
      environment: {
        TABLE_NAME: props.conversationsTable.tableName,
        DOCS_BUCKET: props.documentsBucket.bucketName,
        MAX_UPLOAD_BYTES: '26214400',
      },
    });
    const documents = createFunction({
      id: 'DocumentsFunction',
      handler: 'documents.ts',
      memorySize: 256,
      timeout: Duration.seconds(15),
      environment: {
        TABLE_NAME: props.conversationsTable.tableName,
        DOCS_BUCKET: props.documentsBucket.bucketName,
      },
    });
    const sessions = createFunction({
      id: 'SessionsFunction',
      handler: 'sessions.ts',
      memorySize: 256,
      timeout: Duration.seconds(15),
      environment: {
        TABLE_NAME: props.conversationsTable.tableName,
      },
    });

    const knowledgeBaseArn = `arn:${this.partition}:bedrock:${this.region}:${this.account}:knowledge-base/${props.knowledgeBaseId}`;
    const tableResources = [
      props.conversationsTable.tableArn,
      `${props.conversationsTable.tableArn}/index/gsi1`,
    ];
    const uploadObjects = `${props.documentsBucket.bucketArn}/uploads/*`;
    const keyArn = props.dataKey.keyArn;

    chat.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:Retrieve'],
        resources: [knowledgeBaseArn],
      }),
    );
    const baseModelId = props.chatModelId.replace(/^[a-z]+\./, '');
    // Inference profiles authorize against both the profile and their underlying foundation
    // models; omitting either ARN causes an otherwise opaque AccessDeniedException.
    chat.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock:InvokeModelWithResponseStream',
          'bedrock:InvokeModel',
        ],
        resources: [
          `arn:${this.partition}:bedrock:${this.region}:${this.account}:inference-profile/${props.chatModelId}`,
          `arn:${this.partition}:bedrock:*::foundation-model/${baseModelId}`,
        ],
      }),
    );
    chat.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'dynamodb:Query',
          'dynamodb:PutItem',
          'dynamodb:UpdateItem',
          'dynamodb:GetItem',
        ],
        resources: tableResources,
      }),
    );
    chat.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: [keyArn],
      }),
    );

    ingest.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:IngestKnowledgeBaseDocuments'],
        resources: [knowledgeBaseArn],
      }),
    );
    ingest.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:UpdateItem', 'dynamodb:GetItem'],
        resources: [props.conversationsTable.tableArn],
      }),
    );
    ingest.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:DeleteObject'],
        resources: [uploadObjects],
      }),
    );
    ingest.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: [keyArn],
      }),
    );

    reconciler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:GetKnowledgeBaseDocuments'],
        resources: [knowledgeBaseArn],
      }),
    );
    reconciler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:Query', 'dynamodb:UpdateItem'],
        resources: tableResources,
      }),
    );
    reconciler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:DeleteObject'],
        resources: [uploadObjects],
      }),
    );
    reconciler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: [keyArn],
      }),
    );

    presign.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:PutObject'],
        resources: [uploadObjects],
      }),
    );
    presign.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:PutItem'],
        resources: [props.conversationsTable.tableArn],
      }),
    );
    presign.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt', 'kms:GenerateDataKey'],
        resources: [keyArn],
      }),
    );

    documents.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:Query', 'dynamodb:GetItem'],
        resources: tableResources,
      }),
    );
    documents.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [uploadObjects],
      }),
    );
    documents.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt'],
        resources: [keyArn],
      }),
    );

    sessions.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['dynamodb:Query', 'dynamodb:GetItem'],
        resources: tableResources,
      }),
    );
    sessions.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt'],
        resources: [keyArn],
      }),
    );

    const api = new apigatewayv2.HttpApi(this, 'HttpApi', {
      corsPreflight: {
        allowOrigins: [props.frontendOrigin],
        allowMethods: [
          apigatewayv2.CorsHttpMethod.GET,
          apigatewayv2.CorsHttpMethod.POST,
          apigatewayv2.CorsHttpMethod.OPTIONS,
        ],
        allowHeaders: ['authorization', 'content-type'],
      },
    });
    const authorizer = new HttpUserPoolAuthorizer(
      'CognitoAuthorizer',
      props.userPool,
      { userPoolClients: [props.userPoolClient] },
    );
    const routeOptions = {
      authorizer,
      authorizationScopes: ['kb-api/access'],
    };
    api.addRoutes({
      path: '/v1/uploads',
      methods: [apigatewayv2.HttpMethod.POST],
      integration: new HttpLambdaIntegration('PresignIntegration', presign),
      ...routeOptions,
    });
    api.addRoutes({
      path: '/v1/documents',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: new HttpLambdaIntegration(
        'DocumentsListIntegration',
        documents,
      ),
      ...routeOptions,
    });
    api.addRoutes({
      path: '/v1/documents/{documentId}/download',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: new HttpLambdaIntegration(
        'DocumentsDownloadIntegration',
        documents,
      ),
      ...routeOptions,
    });
    api.addRoutes({
      path: '/v1/sessions',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: new HttpLambdaIntegration('SessionsListIntegration', sessions),
      ...routeOptions,
    });
    api.addRoutes({
      path: '/v1/sessions/{sessionId}',
      methods: [apigatewayv2.HttpMethod.GET],
      integration: new HttpLambdaIntegration(
        'SessionDetailIntegration',
        sessions,
      ),
      ...routeOptions,
    });

    // authType NONE is required because browsers cannot SigV4-sign a request. The handler
    // verifies the Cognito access token itself. This URL is publicly invokable: CORS is a
    // browser convention, not access control. Reserved concurrency caps how many
    // unauthenticated requests can execute at once; the invocation-count alarm is
    // observational only and does not itself stop spend.
    const chatUrl = chat.addFunctionUrl({
      authType: lambda.FunctionUrlAuthType.NONE,
      invokeMode: lambda.InvokeMode.RESPONSE_STREAM,
      cors: {
        allowedOrigins: [props.frontendOrigin],
        allowedMethods: [lambda.HttpMethod.POST],
        allowedHeaders: ['authorization', 'content-type'],
      },
    });

    new events.Rule(this, 'DocumentCreatedRule', {
      eventPattern: {
        source: ['aws.s3'],
        detailType: ['Object Created'],
        detail: {
          bucket: { name: [props.documentsBucket.bucketName] },
        },
      },
      targets: [new targets.LambdaFunction(ingest)],
    });
    new events.Rule(this, 'ReconcilerSchedule', {
      schedule: events.Schedule.rate(Duration.minutes(1)),
      targets: [new targets.LambdaFunction(reconciler)],
    });

    const alarm = (
      alarmId: string,
      metric: cloudwatch.Metric,
      threshold: number,
    ): void => {
      new cloudwatch.Alarm(this, alarmId, {
        metric,
        threshold,
        evaluationPeriods: 1,
        comparisonOperator:
          cloudwatch.ComparisonOperator.GREATER_THAN_OR_EQUAL_TO_THRESHOLD,
      });
    };
    const metricOptions = {
      period: Duration.minutes(5),
      statistic: 'sum',
    };
    alarm('ChatErrorsAlarm', chat.metricErrors(metricOptions), 1);
    alarm('IngestErrorsAlarm', ingest.metricErrors(metricOptions), 1);
    alarm('ReconcilerErrorsAlarm', reconciler.metricErrors(metricOptions), 1);
    alarm('ChatThrottlesAlarm', chat.metricThrottles(metricOptions), 1);
    alarm('ChatInvocationsAlarm', chat.metricInvocations(metricOptions), 500);

    new CfnOutput(this, 'HttpApiUrl', { value: api.apiEndpoint });
    new CfnOutput(this, 'ChatFunctionUrl', { value: chatUrl.url });
  }
}
