import { provideHttpClient } from '@angular/common/http'
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing'
import { ComponentFixture, TestBed } from '@angular/core/testing'
import { provideRouter } from '@angular/router'
import { ReviewQueueComponent } from './review-queue.component'

describe('ReviewQueueComponent', () => {
  let fixture: ComponentFixture<ReviewQueueComponent>
  let component: ReviewQueueComponent
  let http: HttpTestingController

  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [ReviewQueueComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        provideRouter([]),
      ],
    })
    http = TestBed.inject(HttpTestingController)
    fixture = TestBed.createComponent(ReviewQueueComponent)
    component = fixture.componentInstance
    fixture.detectChanges()
  })

  afterEach(() => {
    fixture.destroy()
    http.verify()
  })

  function request() {
    return http.expectOne((req) => req.url.endsWith('review_queue/'))
  }

  it('loads pending records and uses the server permission-filtered count', () => {
    const req = request()
    expect(req.request.params.get('state')).toBe('pending')
    expect(req.request.params.get('page')).toBe('1')
    expect(req.request.params.get('page_size')).toBe('20')
    req.flush({
      count: 27,
      results: [{ document: 12, state: 'pending', reviewer: 42, revision: 1 }],
    })
    fixture.detectChanges()
    expect(fixture.nativeElement.textContent).toContain('27 documents')
    expect(
      fixture.nativeElement.querySelector('tbody a').getAttribute('href')
    ).toBe('/documents/12/review')
  })

  it('resets page and applies state, numeric reviewer, and page size filters', () => {
    request().flush({ count: 0, results: [] })
    component.page = 3
    component.form.setValue({ state: 'rejected', reviewer: 42, pageSize: 50 })
    component.applyFilters()
    const req = request()
    expect(req.request.params.get('state')).toBe('rejected')
    expect(req.request.params.get('reviewer')).toBe('42')
    expect(req.request.params.get('page')).toBe('1')
    expect(req.request.params.get('page_size')).toBe('50')
    req.flush({ count: 0, results: [] })
  })

  it('paginates using the applied filters, not unsaved form edits', () => {
    request().flush({ count: 50, results: [] })
    component.form.controls.state.setValue('approved')
    component.changePage(2)
    const req = request()
    expect(req.request.params.get('page')).toBe('2')
    expect(req.request.params.get('state')).toBe('pending')
    req.flush({ count: 50, results: [] })
  })

  it('allows clearing both filters without enumerating user lists', () => {
    request().flush({ count: 0, results: [] })
    component.form.patchValue({ state: '', reviewer: null })
    component.applyFilters()
    const req = request()
    expect(req.request.params.has('state')).toBe(false)
    expect(req.request.params.has('reviewer')).toBe(false)
    req.flush({ count: 0, results: [] })
  })

  it.each([-1, 0, 1.5])(
    'rejects invalid reviewer ID %s without a request',
    (reviewer) => {
      request().flush({ count: 0, results: [] })
      component.form.controls.reviewer.setValue(reviewer)
      component.applyFilters()
      expect(component.error).toContain('positive integer')
      http.expectNone((req) => req.url.endsWith('review_queue/'))
    }
  )

  it.each([0, 101, 1.5])('rejects invalid page size %s', (pageSize) => {
    request().flush({ count: 0, results: [] })
    component.form.controls.pageSize.setValue(pageSize)
    component.applyFilters()
    expect(component.error).toContain('page size')
    http.expectNone((req) => req.url.endsWith('review_queue/'))
  })

  it('shows an accessible error without stale results on a failed request', () => {
    request().flush({
      count: 1,
      results: [{ document: 12, state: 'pending', reviewer: 42, revision: 1 }],
    })
    component.load()
    request().flush({}, { status: 403, statusText: 'Forbidden' })
    fixture.detectChanges()
    expect(
      fixture.nativeElement.querySelector('[role="alert"]').textContent
    ).toContain('Unable to load')
    expect(component.count).toBe(0)
    expect(component.items).toEqual([])
    expect(fixture.nativeElement.querySelector('tbody a')).toBeNull()
  })

  it('cancels earlier queue requests so stale filters cannot replace new results', () => {
    const old = request()
    component.form.controls.state.setValue('approved')
    component.applyFilters()
    expect(old.cancelled).toBe(true)
    request().flush({ count: 0, results: [] })
  })
})
