import {
  Stack,
  type StackProps,
  aws_bedrock as bedrock,
  aws_iam as iam,
} from 'aws-cdk-lib';
import type {
  aws_kms as kms,
  aws_s3 as s3,
} from 'aws-cdk-lib';
import type { Construct } from 'constructs';

export type KbKnowledgeBaseStackProps = StackProps & {
  documentsBucket: s3.Bucket;
  dataKey: kms.IKey;
  vectorBucketArn: string;
  vectorIndexArn: string;
  embeddingModelId: string;
  embeddingDimension: number;
};

export class KbKnowledgeBaseStack extends Stack {
  public readonly knowledgeBaseId: string;
  public readonly dataSourceId: string;

  public constructor(
    scope: Construct,
    id: string,
    props: KbKnowledgeBaseStackProps,
  ) {
    super(scope, id, props);

    const knowledgeBaseRole = new iam.Role(this, 'KnowledgeBaseRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: {
            'aws:SourceArn': `arn:${this.partition}:bedrock:${this.region}:${this.account}:knowledge-base/*`,
          },
        },
      }),
    });
    knowledgeBaseRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['bedrock:InvokeModel'],
        resources: [
          `arn:${this.partition}:bedrock:${this.region}::foundation-model/${props.embeddingModelId}`,
        ],
      }),
    );
    knowledgeBaseRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:GetObject'],
        resources: [`${props.documentsBucket.bucketArn}/*`],
      }),
    );
    knowledgeBaseRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['s3:ListBucket'],
        resources: [props.documentsBucket.bucketArn],
      }),
    );
    knowledgeBaseRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ['kms:Decrypt'],
        resources: [props.dataKey.keyArn],
      }),
    );
    knowledgeBaseRole.addToPolicy(
      new iam.PolicyStatement({
        actions: [
          's3vectors:PutVectors',
          's3vectors:GetVectors',
          's3vectors:DeleteVectors',
          's3vectors:QueryVectors',
          's3vectors:GetIndex',
        ],
        resources: [props.vectorIndexArn],
      }),
    );

    const knowledgeBase = new bedrock.CfnKnowledgeBase(
      this,
      'KnowledgeBase',
      {
        name: `kb-assistant-${this.account}`,
        roleArn: knowledgeBaseRole.roleArn,
        knowledgeBaseConfiguration: {
          type: 'VECTOR',
          vectorKnowledgeBaseConfiguration: {
            embeddingModelArn: `arn:${this.partition}:bedrock:${this.region}::foundation-model/${props.embeddingModelId}`,
            embeddingModelConfiguration: {
              bedrockEmbeddingModelConfiguration: {
                dimensions: props.embeddingDimension,
                embeddingDataType: 'FLOAT32',
              },
            },
          },
        },
        storageConfiguration: {
          type: 'S3_VECTORS',
          s3VectorsConfiguration: {
            indexArn: props.vectorIndexArn,
            vectorBucketArn: props.vectorBucketArn,
          },
        },
      },
    );
    knowledgeBase.node.addDependency(knowledgeBaseRole);
    knowledgeBase.node.addDependency(
      knowledgeBaseRole.node.findChild('DefaultPolicy'),
    );

    const dataSource = new bedrock.CfnDataSource(this, 'DataSource', {
      name: 'documents-s3',
      knowledgeBaseId: knowledgeBase.attrKnowledgeBaseId,
      dataSourceConfiguration: {
        type: 'S3',
        s3Configuration: {
          bucketArn: props.documentsBucket.bucketArn,
        },
      },
      vectorIngestionConfiguration: {
        chunkingConfiguration: {
          chunkingStrategy: 'FIXED_SIZE',
          // 20% deliberately increases the old 64-token (12.5%) overlap for better recall
          // on Chinese text and fits Bedrock's overlapPercentage granularity.
          fixedSizeChunkingConfiguration: {
            maxTokens: 512,
            overlapPercentage: 20,
          },
        },
      },
      dataDeletionPolicy: 'DELETE',
    });
    dataSource.addDependency(knowledgeBase);

    this.knowledgeBaseId = knowledgeBase.attrKnowledgeBaseId;
    this.dataSourceId = dataSource.attrDataSourceId;
  }
}
