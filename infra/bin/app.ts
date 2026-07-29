#!/usr/bin/env node
import { App } from 'aws-cdk-lib';

import { KbApiStack } from '../lib/api-stack';
import { KbAuthStack } from '../lib/auth-stack';
import { KbKnowledgeBaseStack } from '../lib/knowledge-base-stack';
import { KbStorageStack } from '../lib/storage-stack';

const app = new App();
const env = {
  account: process.env.CDK_DEFAULT_ACCOUNT ?? '123456789012',
  region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1',
};

const frontendOrigin = app.node.getContext('frontendOrigin') as string;
const cognitoDomainPrefix = app.node.getContext(
  'cognitoDomainPrefix',
) as string;
const embeddingModelId = app.node.getContext('embeddingModelId') as string;
const embeddingDimension = Number(app.node.getContext('embeddingDimension'));
const chatModelId = app.node.getContext('chatModelId') as string;

if (
  embeddingModelId === 'amazon.titan-embed-text-v2:0' &&
  ![1024, 512, 256].includes(embeddingDimension)
) {
  throw new Error(
    'embeddingDimension must be 1024, 512, or 256 for amazon.titan-embed-text-v2:0',
  );
}

const storage = new KbStorageStack(app, 'KbStorageStack', {
  env,
  frontendOrigin,
  embeddingDimension,
});
const knowledgeBase = new KbKnowledgeBaseStack(
  app,
  'KbKnowledgeBaseStack',
  {
    env,
    documentsBucket: storage.documentsBucket,
    dataKey: storage.dataKey,
    vectorBucketArn: storage.vectorBucketArn,
    vectorIndexArn: storage.vectorIndexArn,
    embeddingModelId,
    embeddingDimension,
  },
);
const auth = new KbAuthStack(app, 'KbAuthStack', {
  env,
  frontendOrigin,
  cognitoDomainPrefix,
});
new KbApiStack(app, 'KbApiStack', {
  env,
  frontendOrigin,
  chatModelId,
  documentsBucket: storage.documentsBucket,
  conversationsTable: storage.conversationsTable,
  dataKey: storage.dataKey,
  knowledgeBaseId: knowledgeBase.knowledgeBaseId,
  dataSourceId: knowledgeBase.dataSourceId,
  userPool: auth.userPool,
  userPoolClient: auth.userPoolClient,
});
