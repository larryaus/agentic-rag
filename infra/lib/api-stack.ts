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

import { createChatGuardrail } from './chat-guardrail';
import { LAMBDA_ADAPTER_LAYER_VERSION, pythonBackendCode } from './python-backend';

// Each chat turn's log record publishes its metrics here, through CloudWatch Embedded
// Metric Format (backend/src/kb_assistant/metrics.py).
export const METRICS_NAMESPACE = 'KbAssistant';

export type KbApiStackProps = StackProps & {
  frontendOrigins: string[];
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
  retryAttempts?: number;
};

export class KbApiStack extends Stack {
  public constructor(
    scope: Construct,
    id: string,
    props: KbApiStackProps,
  ) {
    super(scope, id, props);

    const code = pythonBackendCode();
    const adapter = lambda.LayerVersion.fromLayerVersionArn(this, 'LambdaWebAdapter',
      `arn:aws:lambda:${this.region}:753240598075:layer:LambdaAdapterLayerArm64:${LAMBDA_ADAPTER_LAYER_VERSION}`);
    const issuer = `https://cognito-idp.${this.region}.amazonaws.com/${props.userPool.userPoolId}`;
    const createFunction = (options: FunctionOptions): lambda.Function => {
      const http = options.handler === 'run.sh';
      const logGroup = new logs.LogGroup(this, `${options.id}LogGroup`, {
        retention: logs.RetentionDays.ONE_WEEK,
        removalPolicy: RemovalPolicy.DESTROY,
      });
      return new lambda.Function(this, options.id, {
        code,
        handler: options.handler,
        runtime: lambda.Runtime.PYTHON_3_12,
        ...(http ? { layers: [adapter] } : {}),
        architecture: lambda.Architecture.ARM_64,
        tracing: lambda.Tracing.ACTIVE,
        logGroup,
        memorySize: options.memorySize,
        timeout: options.timeout,
        environment: {
          ...options.environment,
          DEPLOY_TARGET: 'aws',
          ...(http ? {
            AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap',
            AWS_LWA_PORT: '8080',
            AWS_LWA_READINESS_CHECK_PATH: '/healthz',
            OIDC_ISSUER: issuer,
            OIDC_JWKS_URL: `${issuer}/.well-known/jwks.json`,
            OIDC_CLIENT_ID: props.userPoolClient.userPoolClientId,
            OIDC_REQUIRED_SCOPE: 'kb-api/access',
          } : {}),
          ...(options.id === 'ChatFunction' ? { AWS_LWA_INVOKE_MODE: 'response_stream' } : {}),
        },
        ...(options.reservedConcurrentExecutions === undefined
          ? {}
          : {
              reservedConcurrentExecutions:
                options.reservedConcurrentExecutions,
            }),
        ...(options.retryAttempts === undefined
          ? {}
          : { retryAttempts: options.retryAttempts }),
      });
    };

    const guardrail = createChatGuardrail(this);
    const chat = createFunction({
      id: 'ChatFunction',
      handler: 'run.sh',
      memorySize: 1024,
      timeout: Duration.minutes(5),
      reservedConcurrentExecutions: 5,
      environment: {
        KB_ROUTES: 'chat',
        TABLE_NAME: props.conversationsTable.tableName,
        KNOWLEDGE_BASE_ID: props.knowledgeBaseId,
        CHAT_MODEL_ID: props.chatModelId,
        GUARDRAIL_ID: guardrail.guardrailId,
        GUARDRAIL_VERSION: guardrail.version,
        USER_POOL_ID: props.userPool.userPoolId,
        USER_POOL_CLIENT_ID: props.userPoolClient.userPoolClientId,
        RETRIEVAL_TOP_K: '8',
        MAX_TOOL_ITERATIONS: '6',
        MAX_HISTORY_MESSAGES: '20',
        SESSION_TTL_DAYS: '90',
        METRICS_NAMESPACE,
      },
    });
    const ingest = createFunction({
      id: 'IngestFunction',
      handler: 'kb_assistant.adapters.aws.lambda_events.ingest_handler',
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
      handler: 'kb_assistant.adapters.aws.lambda_events.reconciler_handler',
      memorySize: 512,
      timeout: Duration.minutes(2),
      reservedConcurrentExecutions: 1,
      // The minute schedule retries failed sweeps; async retries would queue duplicates.
      retryAttempts: 0,
      environment: {
        TABLE_NAME: props.conversationsTable.tableName,
        KNOWLEDGE_BASE_ID: props.knowledgeBaseId,
        DATA_SOURCE_ID: props.dataSourceId,
        DOCS_BUCKET: props.documentsBucket.bucketName,
        ABANDONED_UPLOAD_MINUTES: '10',
        MAX_UPLOAD_BYTES: '26214400',
      },
    });
    const presign = createFunction({
      id: 'PresignFunction',
      handler: 'run.sh',
      memorySize: 256,
      timeout: Duration.seconds(15),
      environment: {
        KB_ROUTES: 'uploads',
        TABLE_NAME: props.conversationsTable.tableName,
        DOCS_BUCKET: props.documentsBucket.bucketName,
        MAX_UPLOAD_BYTES: '26214400',
      },
    });
    const documents = createFunction({
      id: 'DocumentsFunction',
      handler: 'run.sh',
      memorySize: 256,
      timeout: Duration.seconds(15),
      environment: {
        KB_ROUTES: 'documents',
        TABLE_NAME: props.conversationsTable.tableName,
        DOCS_BUCKET: props.documentsBucket.bucketName,
      },
    });
    const sessions = createFunction({
      id: 'SessionsFunction',
      handler: 'run.sh',
      memorySize: 256,
      timeout: Duration.seconds(15),
      environment: {
        KB_ROUTES: 'sessions',
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
        actions: ['bedrock:ApplyGuardrail'],
        resources: [guardrail.guardrailArn],
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

    // Direct ingestion authorizes against StartIngestionJob as well as its own action;
    // without both, Bedrock rejects IngestKnowledgeBaseDocuments with AccessDenied.
    ingest.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          'bedrock:IngestKnowledgeBaseDocuments',
          'bedrock:StartIngestionJob',
        ],
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
        actions: [
          'bedrock:GetKnowledgeBaseDocuments',
          'bedrock:IngestKnowledgeBaseDocuments',
          'bedrock:StartIngestionJob',
        ],
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
        actions: ['s3:GetObject', 's3:DeleteObject'],
        resources: [uploadObjects],
      }),
    );
    // S3 returns 404 for a missing HEAD only when the caller can list the
    // bucket; otherwise a missing object and access denial both return 403.
    reconciler.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.documentsBucket.bucketArn],
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
        actions: ['dynamodb:DeleteItem'],
        resources: [props.conversationsTable.tableArn],
      }),
    );
    documents.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject', 's3:DeleteObject'],
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
        allowOrigins: props.frontendOrigins,
        allowMethods: [
          apigatewayv2.CorsHttpMethod.GET,
          apigatewayv2.CorsHttpMethod.POST,
          apigatewayv2.CorsHttpMethod.DELETE,
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
      path: '/v1/documents/{documentId}',
      methods: [apigatewayv2.HttpMethod.DELETE],
      integration: new HttpLambdaIntegration(
        'DocumentsRemoveIntegration',
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
        allowedOrigins: props.frontendOrigins,
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

    const turnMetric = (
      metricName: string,
      statistic: string,
      label?: string,
    ): cloudwatch.Metric =>
      new cloudwatch.Metric({
        namespace: METRICS_NAMESPACE,
        metricName,
        dimensionsMap: { model: props.chatModelId },
        statistic,
        ...(label === undefined ? {} : { label }),
        period: Duration.minutes(5),
      });
    // A failed turn still returns 200 with an error event, so the function's error count
    // never sees it. The turn's own record does.
    alarm(
      'ChatFailedTurnsAlarm',
      turnMetric('turns', 'Sum').with({
        dimensionsMap: { model: props.chatModelId, stopReason: 'error' },
      }),
      1,
    );

    // CDK's widget classes declare `warnings` as possibly undefined, which IWidget's
    // optional property rejects under exactOptionalPropertyTypes.
    const graph = (graphProps: cloudwatch.GraphWidgetProps): cloudwatch.IWidget =>
      new cloudwatch.GraphWidget(graphProps) as cloudwatch.IWidget;
    const dashboard = new cloudwatch.Dashboard(this, 'Dashboard', {
      dashboardName: 'KbAssistant',
      defaultInterval: Duration.days(1),
    });
    dashboard.addWidgets(
      graph({
        title: 'Chat turns by stop reason',
        width: 12,
        stacked: true,
        left: [
          new cloudwatch.MathExpression({
            // A search finds every stop reason, including ones no one listed here.
            expression: `SEARCH('{${METRICS_NAMESPACE},model,stopReason} MetricName="turns" model="${props.chatModelId}"', 'Sum', 300)`,
            usingMetrics: {},
            label: '',
            period: Duration.minutes(5),
          }),
        ],
      }),
      graph({
        title: 'Answer latency (ms)',
        width: 12,
        left: [
          turnMetric('firstTextMs', 'p50', 'First text p50'),
          turnMetric('firstTextMs', 'p90', 'First text p90'),
          turnMetric('turnMs', 'p50', 'Whole turn p50'),
          turnMetric('turnMs', 'p90', 'Whole turn p90'),
        ],
      }),
    );
    dashboard.addWidgets(
      graph({
        title: 'Model tokens',
        width: 6,
        left: [
          turnMetric('inputTokens', 'Sum', 'Input'),
          turnMetric('outputTokens', 'Sum', 'Output'),
        ],
      }),
      graph({
        title: 'Searches per turn',
        width: 6,
        left: [
          turnMetric('toolCalls', 'Average', 'Average'),
          turnMetric('toolCalls', 'Maximum', 'Most'),
        ],
      }),
      graph({
        title: 'Chat function',
        width: 6,
        left: [
          chat.metricInvocations({ ...metricOptions, label: 'Invocations' }),
          chat.metricErrors({ ...metricOptions, label: 'Errors' }),
          chat.metricThrottles({ ...metricOptions, label: 'Throttles' }),
        ],
        right: [chat.metricDuration({ period: Duration.minutes(5), statistic: 'p90', label: 'Duration p90 (ms)' })],
      }),
      graph({
        title: 'Document ingestion',
        width: 6,
        left: [
          ingest.metricInvocations({ ...metricOptions, label: 'Ingest invocations' }),
          ingest.metricErrors({ ...metricOptions, label: 'Ingest errors' }),
          reconciler.metricErrors({ ...metricOptions, label: 'Reconciler errors' }),
        ],
      }),
    );

    new CfnOutput(this, 'HttpApiUrl', { value: api.apiEndpoint });
    new CfnOutput(this, 'ChatFunctionUrl', { value: chatUrl.url });
    new CfnOutput(this, 'DashboardUrl', {
      value: `https://console.aws.amazon.com/cloudwatch/home?region=${this.region}#dashboards/dashboard/${dashboard.dashboardName}`,
    });
  }
}
