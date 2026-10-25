import { createHash } from 'node:crypto';

import { aws_bedrock as bedrock } from 'aws-cdk-lib';
import type { Construct } from 'constructs';

// Medium rather than high: a handbook assistant is asked about harassment, misconduct
// and safety policy, and the strictest setting is the likeliest to refuse those.
const HARMFUL_CONTENT = ['HATE', 'INSULTS', 'MISCONDUCT', 'SEXUAL', 'VIOLENCE'];

// Financial data, credentials and government identifiers. Contact details are left out
// on purpose: documents legitimately hold them, such as a support address to write to.
const MASKED_IN_ANSWERS = [
  'CREDIT_DEBIT_CARD_NUMBER',
  'CREDIT_DEBIT_CARD_CVV',
  'CREDIT_DEBIT_CARD_EXPIRY',
  'PIN',
  'INTERNATIONAL_BANK_ACCOUNT_NUMBER',
  'SWIFT_CODE',
  'US_BANK_ACCOUNT_NUMBER',
  'US_BANK_ROUTING_NUMBER',
  'PASSWORD',
  'AWS_ACCESS_KEY',
  'AWS_SECRET_KEY',
  'US_SOCIAL_SECURITY_NUMBER',
  'US_INDIVIDUAL_TAX_IDENTIFICATION_NUMBER',
  'US_PASSPORT_NUMBER',
  'DRIVER_ID',
  'UK_NATIONAL_INSURANCE_NUMBER',
  'UK_NATIONAL_HEALTH_SERVICE_NUMBER',
  'UK_UNIQUE_TAXPAYER_REFERENCE_NUMBER',
  'CA_SOCIAL_INSURANCE_NUMBER',
  'CA_HEALTH_NUMBER',
];

export type ChatGuardrail = {
  guardrailId: string;
  guardrailArn: string;
  version: string;
};

/**
 * Checks each question for harmful content and prompt attacks, and each answer for
 * harmful content and sensitive data, which it replaces with a placeholder.
 */
export function createChatGuardrail(scope: Construct): ChatGuardrail {
  const policy = {
    blockedInputMessaging:
      "I can't help with that request. Please ask a question about the documents in the knowledge base.",
    blockedOutputsMessaging:
      "I can't share that answer. Please try rephrasing your question.",
    contentPolicyConfig: {
      filtersConfig: [
        ...HARMFUL_CONTENT.map((type) => ({
          type,
          inputStrength: 'MEDIUM',
          outputStrength: 'MEDIUM',
        })),
        // Prompt attacks are checked on the way in only; the output strength must be NONE.
        { type: 'PROMPT_ATTACK', inputStrength: 'HIGH', outputStrength: 'NONE' },
      ],
    },
    sensitiveInformationPolicyConfig: {
      // Answers only. What a user types about themselves is theirs to send, and
      // checking it would charge for every question.
      piiEntitiesConfig: MASKED_IN_ANSWERS.map((type) => ({
        type,
        action: 'ANONYMIZE',
        inputEnabled: false,
        outputEnabled: true,
        outputAction: 'ANONYMIZE',
      })),
    },
  };
  const guardrail = new bedrock.CfnGuardrail(scope, 'ChatGuardrail', {
    name: 'kb-assistant-chat',
    description:
      'Content filters and a prompt-attack check for the chat assistant, and masking of sensitive data in its answers',
    ...policy,
  });

  // A version is an immutable snapshot, so CloudFormation never updates one in place.
  // Keying the logical ID on the policy makes every policy change publish a new version.
  const policyHash = createHash('sha256')
    .update(JSON.stringify(policy))
    .digest('hex')
    .slice(0, 8);
  const version = new bedrock.CfnGuardrailVersion(
    scope,
    `ChatGuardrailVersion${policyHash}`,
    { guardrailIdentifier: guardrail.attrGuardrailId },
  );

  return {
    guardrailId: guardrail.attrGuardrailId,
    guardrailArn: guardrail.attrGuardrailArn,
    version: version.attrVersion,
  };
}
