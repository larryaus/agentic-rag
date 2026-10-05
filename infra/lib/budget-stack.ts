import { Stack, type StackProps, aws_budgets as budgets } from 'aws-cdk-lib';
import type { Construct } from 'constructs';

export type KbBudgetStackProps = StackProps & {
  alertEmail: string;
  monthlyLimitUsd: number;
};

export class KbBudgetStack extends Stack {
  public constructor(scope: Construct, id: string, props: KbBudgetStackProps) {
    super(scope, id, props);

    const subscribers = [
      { subscriptionType: 'EMAIL', address: props.alertEmail },
    ];
    const alert = (
      notificationType: 'ACTUAL' | 'FORECASTED',
      threshold: number,
    ): budgets.CfnBudget.NotificationWithSubscribersProperty => ({
      notification: {
        notificationType,
        comparisonOperator: 'GREATER_THAN',
        threshold,
        thresholdType: 'PERCENTAGE',
      },
      subscribers,
    });

    // Covers the whole account, not only this app: Bedrock tokens are the cost that
    // can run away, and they are not tagged per stack. This notifies; it does not cap
    // spend, and billing data lags by several hours.
    new budgets.CfnBudget(this, 'MonthlyBudget', {
      budget: {
        budgetName: 'kb-assistant-monthly',
        budgetType: 'COST',
        timeUnit: 'MONTHLY',
        budgetLimit: { amount: props.monthlyLimitUsd, unit: 'USD' },
      },
      notificationsWithSubscribers: [
        alert('ACTUAL', 80),
        alert('ACTUAL', 100),
        alert('FORECASTED', 100),
      ],
    });
  }
}
