import { provideHttpClient } from '@angular/common/http'
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing'
import { ComponentFixture, TestBed } from '@angular/core/testing'
import { DocumentReview, ReviewAction } from 'src/app/data/document-review'
import { environment } from 'src/environments/environment'
import { DocumentReviewComponent } from './document-review.component'

describe('DocumentReviewComponent', () => {
  let fixture: ComponentFixture<DocumentReviewComponent>
  let component: DocumentReviewComponent
  let http: HttpTestingController
  let review: DocumentReview
  const url = `${environment.apiBaseUrl}documents/12/review/`

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [DocumentReviewComponent],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    })
    http = TestBed.inject(HttpTestingController)
    fixture = TestBed.createComponent(DocumentReviewComponent)
    component = fixture.componentInstance
    review = {
      document: 12,
      state: 'unreviewed',
      revision: 0,
      reviewer: null,
      submitted_by: null,
      history: [],
      allowed_actions: ['submit'],
      eligible_reviewers: [{ id: 42, username: 'eligible' }],
    }
    fixture.componentRef.setInput('documentId', 12)
    fixture.detectChanges()
    http.expectOne(url).flush(review)
    fixture.detectChanges()
  })

  afterEach(() => {
    fixture.destroy()
    http.verify()
  })

  function show(state: Partial<DocumentReview>) {
    component.review.set({ ...review, ...state })
    fixture.changeDetectorRef.markForCheck()
    fixture.detectChanges()
  }

  function button(label: string): HTMLButtonElement {
    return Array.from(
      fixture.nativeElement.querySelectorAll(
        'button'
      ) as NodeListOf<HTMLButtonElement>
    ).find((element) => element.textContent.trim() === label)
  }

  it('shows only backend-authorized actions, not approval merely because state is pending', () => {
    show({
      state: 'pending',
      reviewer: 42,
      submitted_by: 7,
      allowed_actions: [],
    })
    expect(button('Approve')).toBeUndefined()
    expect(button('Reject')).toBeUndefined()
    expect(button('Submit for review')).toBeUndefined()
    component.act('approve')
    http.expectNone(url)
  })

  it('does not grant superuser or self-submitter decisions beyond allowed_actions', () => {
    show({
      state: 'pending',
      reviewer: 7,
      submitted_by: 7,
      allowed_actions: [],
    })
    expect(component.allowed('approve')).toBe(false)
    expect(component.allowed('reject')).toBe(false)
    component.act('reject')
    http.expectNone(url)
  })

  it('lists only backend-eligible reviewers, excluding self/inactive/inaccessible candidates', () => {
    const options = fixture.nativeElement.querySelectorAll(
      '#review-reviewer option'
    )
    expect(options.length).toBe(2)
    expect(options[1].textContent).toBe('eligible')
    expect(button('Submit for review').disabled).toBe(true)
    component.form.controls.reviewer.setValue(7)
    component.act('submit')
    http.expectNone(url)
    component.form.controls.reviewer.setValue(42)
    fixture.detectChanges()
    expect(button('Submit for review').disabled).toBe(false)
  })

  it('cannot submit without an eligible reviewer even when submission is authorized', () => {
    show({ eligible_reviewers: [] })
    component.form.controls.reviewer.setValue(42)
    component.act('submit')
    expect(component.validReviewer).toBe(false)
    http.expectNone(url)
  })

  it('sends revision and UUID with submit, without a rejection comment', () => {
    component.form.controls.reviewer.setValue(42)
    component.form.controls.comment.setValue('not for submit')
    fixture.detectChanges()
    button('Submit for review').click()
    const req = http.expectOne(url)
    expect(req.request.body).toEqual({
      action: 'submit',
      reviewer: 42,
      expected_revision: 0,
      request_id: expect.stringMatching(
        /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
      ),
    })
    req.flush({
      ...review,
      state: 'pending',
      revision: 1,
      reviewer: 42,
      submitted_by: 7,
      allowed_actions: [],
    })
    fixture.detectChanges()
    expect(component.review().revision).toBe(1)
    expect(button('Submit for review')).toBeUndefined()
  })

  it.each<ReviewAction>(['approve', 'reopen'])(
    'sends no reviewer or comment on %s',
    (action) => {
      show({ revision: 4, allowed_actions: [action] })
      component.form.setValue({ reviewer: 42, comment: 'unused' })
      component.act(action)
      const req = http.expectOne(url)
      expect(req.request.body).toEqual({
        action,
        expected_revision: 4,
        request_id: expect.any(String),
      })
      req.flush(review)
    }
  )

  it('validates and trims rejection comments, counting Unicode code points', () => {
    show({ allowed_actions: ['reject'] })
    for (const comment of ['  ', 'a'.repeat(2001)]) {
      component.form.controls.comment.setValue(comment)
      component.act('reject')
      http.expectNone(url)
    }
    component.form.controls.comment.setValue(`  ${'😀'.repeat(2000)}  `)
    expect(component.validComment).toBe(true)
    component.act('reject')
    const req = http.expectOne(url)
    expect(req.request.body.comment).toBe('😀'.repeat(2000))
    expect(req.request.body.reviewer).toBeUndefined()
    req.flush(review)
  })

  it('renders history comments as escaped text, never HTML', () => {
    const comment = '<img src=x onerror="alert(1)"><script>alert(1)</script>'
    show({
      state: 'rejected',
      history: [
        {
          action: 'reject',
          actor: 42,
          at: '2026-01-01T00:00:00Z',
          revision: 2,
          request_id: 'uuid',
          comment,
        },
      ],
    })
    expect(
      fixture.nativeElement.querySelector('.review-comment').textContent.trim()
    ).toBe(comment)
    expect(fixture.nativeElement.querySelector('img, script')).toBeNull()
  })

  it('refetches on conflict and preserves visible conflict feedback with the latest state', () => {
    show({ state: 'pending', revision: 1, allowed_actions: ['approve'] })
    component.act('approve')
    http.expectOne(url).flush({}, { status: 409, statusText: 'Conflict' })
    expect(component.review()).toBeUndefined()
    http.expectOne(url).flush({
      ...review,
      state: 'rejected',
      revision: 2,
      allowed_actions: ['reopen'],
    })
    fixture.detectChanges()
    expect(component.review().revision).toBe(2)
    expect(
      fixture.nativeElement.querySelector('[role="status"]').textContent
    ).toContain('review changed')
    expect(button('Approve')).toBeUndefined()
    expect(button('Reopen')).toBeDefined()
    expect(component.retryMutation).toBeNull()
  })

  it('keeps conflict feedback and hides stale controls when refetch fails', () => {
    show({ allowed_actions: ['approve'] })
    component.act('approve')
    http.expectOne(url).flush({}, { status: 409, statusText: 'Conflict' })
    http.expectOne(url).flush({}, { status: 404, statusText: 'Not Found' })
    fixture.detectChanges()
    expect(component.feedback).toContain('could not be loaded')
    expect(button('Approve')).toBeUndefined()
  })

  it.each([0, 503])(
    'retries uncertain status %s with the same UUID and exact payload',
    (status) => {
      show({ allowed_actions: ['reject'] })
      component.form.controls.comment.setValue('original')
      component.act('reject')
      const req = http.expectOne(url)
      const payload = req.request.body
      if (status === 0) req.error(new ProgressEvent('error'))
      else req.flush({}, { status, statusText: 'Unavailable' })
      component.form.controls.comment.setValue('changed after failure')
      component.act('reject')
      http.expectNone(url)
      component.retry()
      const retry = http.expectOne(url)
      expect(retry.request.body).toEqual(payload)
      retry.flush(review)
      expect(component.retryMutation).toBeNull()
    }
  )

  it('disables mutations in trash even if the response contains allowed actions', () => {
    fixture.componentRef.setInput('inTrash', true)
    fixture.detectChanges()
    http.expectOne(url).flush(review)
    fixture.detectChanges()
    component.form.controls.reviewer.setValue(42)
    expect(button('Submit for review')).toBeUndefined()
    component.act('submit')
    http.expectNone(url)
  })

  it('refreshes review after document content metadata changes', () => {
    fixture.componentRef.setInput('refreshToken', 'new modification')
    fixture.detectChanges()
    http.expectOne(url).flush({ ...review, revision: 3 })
    expect(component.review().revision).toBe(3)
  })

  it('cancels old document requests when navigating to another document', () => {
    component.load()
    const old = http.expectOne(url)
    fixture.componentRef.setInput('documentId', 13)
    fixture.detectChanges()
    expect(old.cancelled).toBe(true)
    http
      .expectOne(`${environment.apiBaseUrl}documents/13/review/`)
      .flush({ ...review, document: 13 })
    expect(component.review().document).toBe(13)
  })
})
