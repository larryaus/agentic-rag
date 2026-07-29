import {
  RemovalPolicy,
  Stack,
  type StackProps,
  aws_dynamodb as dynamodb,
  aws_kms as kms,
  aws_s3 as s3,
  aws_s3vectors as s3vectors,
} from 'aws-cdk-lib';
import type { Construct } from 'constructs';

export type KbStorageStackProps = StackProps & {
  frontendOrigin: string;
  embeddingDimension: number;
};

export class KbStorageStack extends Stack {
  public readonly dataKey: kms.Key;
  public readonly documentsBucket: s3.Bucket;
  public readonly conversationsTable: dynamodb.Table;
  public readonly vectorBucketArn: string;
  public readonly vectorIndexArn: string;

  public constructor(
    scope: Construct,
    id: string,
    props: KbStorageStackProps,
  ) {
    super(scope, id, props);

    this.dataKey = new kms.Key(this, 'DataKey', {
      enableKeyRotation: true,
      alias: 'alias/kb-assistant',
      removalPolicy: RemovalPolicy.DESTROY,
    });

    this.documentsBucket = new s3.Bucket(this, 'DocumentsBucket', {
      encryption: s3.BucketEncryption.KMS,
      encryptionKey: this.dataKey,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,
      enforceSSL: true,
      versioned: true,
      removalPolicy: RemovalPolicy.DESTROY,
      autoDeleteObjects: true,
      cors: [
        {
          allowedOrigins: [props.frontendOrigin],
          allowedMethods: [s3.HttpMethods.PUT],
          allowedHeaders: ['*'],
        },
      ],
    });
    const cfnDocumentsBucket = this.documentsBucket.node.defaultChild;
    if (!(cfnDocumentsBucket instanceof s3.CfnBucket)) {
      throw new Error('DocumentsBucket must synthesize an AWS::S3::Bucket');
    }
    // aws-cdk-lib 2.262.1 implements BucketProps.eventBridgeEnabled with a
    // notification custom resource. The native property keeps Storage independent
    // and produces the NotificationConfiguration required by this architecture.
    cfnDocumentsBucket.notificationConfiguration = {
      eventBridgeConfiguration: { eventBridgeEnabled: true },
    };

    const vectorBucket = new s3vectors.CfnVectorBucket(this, 'VectorBucket', {
      vectorBucketName: `kb-vectors-${this.account}-${this.region}`,
    });
    const vectorIndex = new s3vectors.CfnIndex(this, 'VectorIndex', {
      indexName: 'kb-index',
      vectorBucketArn: vectorBucket.attrVectorBucketArn,
      dataType: 'float32',
      dimension: props.embeddingDimension,
      distanceMetric: 'cosine',
    });
    vectorIndex.addDependency(vectorBucket);
    this.vectorBucketArn = vectorBucket.attrVectorBucketArn;
    this.vectorIndexArn = vectorIndex.attrIndexArn;

    this.conversationsTable = new dynamodb.Table(
      this,
      'ConversationsTable',
      {
        partitionKey: { name: 'pk', type: dynamodb.AttributeType.STRING },
        sortKey: { name: 'sk', type: dynamodb.AttributeType.STRING },
        billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
        encryption: dynamodb.TableEncryption.CUSTOMER_MANAGED,
        encryptionKey: this.dataKey,
        pointInTimeRecoverySpecification: {
          pointInTimeRecoveryEnabled: true,
        },
        timeToLiveAttribute: 'ttl',
        removalPolicy: RemovalPolicy.DESTROY,
      },
    );
    this.conversationsTable.addGlobalSecondaryIndex({
      indexName: 'gsi1',
      partitionKey: { name: 'gsi1pk', type: dynamodb.AttributeType.STRING },
      sortKey: { name: 'gsi1sk', type: dynamodb.AttributeType.STRING },
      projectionType: dynamodb.ProjectionType.ALL,
    });
  }
}
