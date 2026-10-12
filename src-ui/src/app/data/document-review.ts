export type ReviewState = 'unreviewed' | 'pending' | 'approved' | 'rejected'
export type ReviewAction = 'submit' | 'approve' | 'reject' | 'reopen'

export interface ReviewHistoryEvent {
  action: ReviewAction | 'invalidate'
  actor: number | null
  revision: number
  at: string
  request_id: string | null
  comment: string
}

export interface DocumentReview {
  document: number
  state: ReviewState
  revision: number
  reviewer: number | null
  submitted_by: number | null
  history: ReviewHistoryEvent[]
  allowed_actions: string[]
  eligible_reviewers: { id: number; username: string }[]
}

export type ReviewMutation = {
  expected_revision: number
  request_id: string
} & (
  | { action: 'submit'; reviewer: number }
  | { action: 'reject'; comment: string }
  | { action: 'approve' | 'reopen' }
)

export interface ReviewQueueItem {
  document: number
  state: ReviewState
  reviewer: number | null
  revision: number
}

export interface ReviewQueue {
  count: number
  results: ReviewQueueItem[]
}

export const REVIEW_STATE_LABELS: Record<ReviewState, string> = {
  unreviewed: $localize`Unreviewed`,
  pending: $localize`Pending`,
  approved: $localize`Approved`,
  rejected: $localize`Rejected`,
}

export const REVIEW_ACTION_LABELS: Record<ReviewAction | 'invalidate', string> =
  {
    submit: $localize`Submit for review`,
    approve: $localize`Approve`,
    reject: $localize`Reject`,
    reopen: $localize`Reopen`,
    invalidate: $localize`Invalidated by content change`,
  }
