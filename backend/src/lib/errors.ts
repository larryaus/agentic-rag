import type { Citation, Usage } from '@kb/shared';

export type PartialAgentResult = {
  text: string;
  citations: Citation[];
  usage: Usage;
  stopReason: 'error';
};

export class AppError extends Error {
  public constructor(
    message: string,
    public readonly statusCode: number,
  ) {
    super(message);
    this.name = new.target.name;
  }
}

export class UnauthorizedError extends AppError {
  public constructor(message = 'Unauthorized') {
    super(message, 401);
  }
}

export class ForbiddenError extends AppError {
  public constructor(message = 'Forbidden') {
    super(message, 403);
  }
}

export class NotFoundError extends AppError {
  public constructor(message = 'Not found') {
    super(message, 404);
  }
}

export class ValidationError extends AppError {
  public constructor(message = 'Invalid request') {
    super(message, 400);
  }
}

export class ConflictError extends AppError {
  public constructor(message = 'Conflict') {
    super(message, 409);
  }
}

export class BedrockStreamError extends Error {
  public constructor(
    message: string,
    public readonly partialResult: PartialAgentResult,
  ) {
    super(message);
    this.name = 'BedrockStreamError';
  }
}

export function errorMessage(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : 'Unknown error';
}
