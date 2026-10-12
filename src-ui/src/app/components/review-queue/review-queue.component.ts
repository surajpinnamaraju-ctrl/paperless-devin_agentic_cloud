import {
  ChangeDetectorRef,
  Component,
  OnDestroy,
  OnInit,
  inject,
} from '@angular/core'
import {
  FormControl,
  FormGroup,
  ReactiveFormsModule,
  Validators,
} from '@angular/forms'
import { RouterLink } from '@angular/router'
import { NgbPaginationModule } from '@ng-bootstrap/ng-bootstrap'
import { Subscription } from 'rxjs'
import {
  REVIEW_STATE_LABELS,
  ReviewQueueItem,
  ReviewState,
} from 'src/app/data/document-review'
import { DocumentReviewService } from 'src/app/services/rest/document-review.service'
import { PageHeaderComponent } from '../common/page-header/page-header.component'

@Component({
  selector: 'pngx-review-queue',
  templateUrl: './review-queue.component.html',
  imports: [
    ReactiveFormsModule,
    RouterLink,
    NgbPaginationModule,
    PageHeaderComponent,
  ],
})
export class ReviewQueueComponent implements OnInit, OnDestroy {
  private service = inject(DocumentReviewService)
  private changeDetector = inject(ChangeDetectorRef)
  private request: Subscription
  stateLabels = REVIEW_STATE_LABELS
  states = Object.keys(REVIEW_STATE_LABELS) as ReviewState[]
  items: ReviewQueueItem[] = []
  count = 0
  page = 1
  pageSize = 20
  busy = false
  error = ''
  private state: ReviewState | '' = 'pending'
  private reviewer: number | null = null
  form = new FormGroup({
    state: new FormControl<ReviewState | ''>('pending', { nonNullable: true }),
    reviewer: new FormControl<number | null>(null, [Validators.min(1)]),
    pageSize: new FormControl(20, {
      nonNullable: true,
      validators: [Validators.min(1), Validators.max(100)],
    }),
  })

  ngOnInit(): void {
    this.load()
  }

  ngOnDestroy(): void {
    this.request?.unsubscribe()
  }

  applyFilters(): void {
    const { state, reviewer, pageSize } = this.form.getRawValue()
    if (
      this.form.invalid ||
      (reviewer != null && !Number.isSafeInteger(reviewer)) ||
      !Number.isInteger(pageSize)
    ) {
      this.error = $localize`Use a positive integer reviewer ID and a page size from 1 to 100.`
      return
    }
    this.state = state
    this.reviewer = reviewer
    this.pageSize = pageSize
    this.page = 1
    this.load()
  }

  changePage(page: number): void {
    this.page = page
    this.load()
  }

  load(): void {
    this.request?.unsubscribe()
    this.busy = true
    this.error = ''
    this.items = []
    this.count = 0
    this.request = this.service
      .queue(this.state, this.reviewer, this.page, this.pageSize)
      .subscribe({
        next: (queue) => {
          this.items = queue.results
          this.count = queue.count
          this.busy = false
          this.changeDetector.markForCheck()
        },
        error: () => {
          this.busy = false
          this.error = $localize`Unable to load the review queue. Check your filters and access, then try again.`
          this.changeDetector.markForCheck()
        },
      })
  }
}
