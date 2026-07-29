/**
 * SDK clients are reused across warm invocations and remain mockable at the client boundary.
 */
import { BedrockAgentClient } from '@aws-sdk/client-bedrock-agent';
import { BedrockAgentRuntimeClient } from '@aws-sdk/client-bedrock-agent-runtime';
import { BedrockRuntimeClient } from '@aws-sdk/client-bedrock-runtime';
import { DynamoDBClient } from '@aws-sdk/client-dynamodb';
import { S3Client } from '@aws-sdk/client-s3';
import { DynamoDBDocumentClient } from '@aws-sdk/lib-dynamodb';

export const bedrockAgentClient = new BedrockAgentClient({});
export const bedrockAgentRuntimeClient = new BedrockAgentRuntimeClient({});
export const bedrockRuntimeClient = new BedrockRuntimeClient({});
export const dynamoClient = DynamoDBDocumentClient.from(new DynamoDBClient({}), {
  marshallOptions: { removeUndefinedValues: true },
});
export const s3Client = new S3Client({
  requestChecksumCalculation: 'WHEN_REQUIRED',
});
