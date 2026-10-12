import { DatePipe } from '@angular/common'
import { HttpErrorResponse } from '@angular/common/http'
import {
  ChangeDetectorRef,
  Component,
  Input,
  OnChanges,
  OnDestroy,
  inject,
  signal,
} from '@angular/core'
import { FormControl, FormGroup, ReactiveFormsModule } from '@angular/forms'
import { Subscription } from 'rxjs'
import {
  DocumentReview,
  REVIEW_ACTION_LABELS,
  REVIEW_STATE_LABELS,
  ReviewAction,
  ReviewMutation,
} from 'src/app/data/document-review'
import { DocumentReviewService } from 'src/app/services/rest/document-review.service'
import { v4 as uuidv4 } from 'uuid'

@Component({
  selector: 'pngx-document-review',
  templateUrl: './document-review.component.html',
  imports: [DatePipe, ReactiveFormsModule],
})
export class DocumentReviewComponent implements OnChanges, OnDestroy {
  private service = inject(DocumentReviewService)
  private changeDetector = inject(ChangeDetectorRef)
  private request: Subscription

  @Input() documentId: number
  @Input() refreshToken: string
  @Input() inTrash = false

  readonly review = signal<DocumentReview>(undefined)
  busy = false
  feedback = ''
  retryMutation: ReviewMutation | null = null
  stateLabels = REVIEW_STATE_LABELS
  actionLabels = REVIEW_ACTION_LABELS
  form = new FormGroup({
    reviewer: new FormControl<number | null>(null),
    comment: new FormControl('', { nonNullable: true }),
  })

  ngOnChanges(): void {
    this.request?.unsubscribe()
    this.retryMutation = null
    this.review.set(undefined)
    this.feedback = ''
    this.form.reset()
    if (this.documentId) this.load()
  }

  ngOnDestroy(): void {
    this.request?.unsubscribe()
  }

  allowed(action: ReviewAction): boolean {
    return !this.inTrash && !!this.review()?.allowed_actions?.includes(action)
  }

  get validReviewer(): boolean {
    return !!this.review()?.eligible_reviewers?.some(
      (reviewer) => reviewer.id === this.form.controls.reviewer.value
    )
  }

  get validComment(): boolean {
    const length = Array.from(this.form.controls.comment.value.trim()).length
    return length >= 1 && length <= 2000
  }

  load(preserveFeedback = false): void {
    this.request?.unsubscribe()
    this.busy = true
    this.retryMutation = null
    this.review.set(undefined)
    if (!preserveFeedback) this.feedback = ''
    this.request = this.service.get(this.documentId).subscribe({
      next: (review) => {
        this.review.set(review)
        this.busy = false
        this.changeDetector.markForCheck()
      },
      error: () => {
        this.busy = false
        this.feedback = preserveFeedback
          ? $localize`The latest review state could not be loaded. Refresh before acting.`
          : $localize`Unable to load review. You may no longer have access to this document.`
        this.changeDetector.markForCheck()
      },
    })
  }

  act(action: ReviewAction): void {
    if (this.busy || this.retryMutation || !this.allowed(action)) return
    if (action === 'submit' && !this.validReviewer) return
    if (action === 'reject' && !this.validComment) return
    const base = {
      expected_revision: this.review().revision,
      request_id: uuidv4(),
    }
    const mutation: ReviewMutation =
      action === 'submit'
        ? { ...base, action, reviewer: this.form.controls.reviewer.value }
        : action === 'reject'
          ? {
              ...base,
              action,
              comment: this.form.controls.comment.value.trim(),
            }
          : { ...base, action }
    this.send(mutation)
  }

  retry(): void {
    if (this.retryMutation && !this.busy) this.send(this.retryMutation)
  }

  private send(mutation: ReviewMutation): void {
    this.busy = true
    this.feedback = ''
    this.request = this.service.mutate(this.documentId, mutation).subscribe({
      next: (review) => {
        this.busy = false
        this.retryMutation = null
        this.review.set(review)
        this.form.reset()
        this.feedback = $localize`Review updated.`
        this.changeDetector.markForCheck()
      },
      error: (error: HttpErrorResponse) => {
        this.busy = false
        this.changeDetector.markForCheck()
        if (error.status === 409) {
          this.feedback = $localize`The review changed or this request conflicts with an earlier action. Latest state reloaded; check it before acting again.`
          this.load(true)
        } else if (error.status === 0 || error.status >= 500) {
          this.retryMutation = mutation
          this.feedback = $localize`The result could not be confirmed. Retry the same request safely, or refresh the state before a new action.`
        } else {
          this.retryMutation = null
          this.feedback = $localize`Review action failed. Check your permissions, reviewer, and rejection comment, then refresh before trying again.`
          this.load(true)
        }
      },
    })
  }
}
