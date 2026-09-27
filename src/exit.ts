/** Process exit codes for the aqa CLI. */
export const EXIT = {
  OK: 0,
  FAILURE: 1,
  INVALID_CONFIG: 2,
} as const;

export type ExitCode = (typeof EXIT)[keyof typeof EXIT];
