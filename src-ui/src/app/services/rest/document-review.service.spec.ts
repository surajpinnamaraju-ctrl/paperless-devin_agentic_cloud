import { provideHttpClient } from '@angular/common/http'
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing'
import { TestBed } from '@angular/core/testing'
import { ReviewMutation } from 'src/app/data/document-review'
import { environment } from 'src/environments/environment'
import { DocumentReviewService } from './document-review.service'

describe('DocumentReviewService', () => {
  let service: DocumentReviewService
  let http: HttpTestingController
  const url = `${environment.apiBaseUrl}documents/12/review/`

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    })
    service = TestBed.inject(DocumentReviewService)
    http = TestBed.inject(HttpTestingController)
  })

  afterEach(() => http.verify())

  it('reads complete review state from the document endpoint', () => {
    service.get(12).subscribe()
    const req = http.expectOne(url)
    expect(req.request.method).toBe('GET')
    req.flush({})
  })

  it.each<ReviewMutation>([
    {
      action: 'submit',
      reviewer: 42,
      expected_revision: 0,
      request_id: '35444c15-fca7-4a1d-8756-2a01dac4d8cc',
    },
    {
      action: 'approve',
      expected_revision: 1,
      request_id: '35444c15-fca7-4a1d-8756-2a01dac4d8cc',
    },
    {
      action: 'reject',
      comment: 'Please correct',
      expected_revision: 1,
      request_id: '35444c15-fca7-4a1d-8756-2a01dac4d8cc',
    },
    {
      action: 'reopen',
      expected_revision: 2,
      request_id: '35444c15-fca7-4a1d-8756-2a01dac4d8cc',
    },
  ])('posts only the supplied mutation payload ($action)', (mutation) => {
    service.mutate(12, mutation).subscribe()
    const req = http.expectOne(url)
    expect(req.request.method).toBe('POST')
    expect(req.request.body).toEqual(mutation)
    req.flush({})
  })

  it('sends queue state/reviewer and pagination as separate filters', () => {
    service.queue('rejected', 42, 3, 50).subscribe()
    const req = http.expectOne(
      (request) => request.url === `${environment.apiBaseUrl}review_queue/`
    )
    expect(req.request.method).toBe('GET')
    expect(req.request.params.keys().sort()).toEqual([
      'page',
      'page_size',
      'reviewer',
      'state',
    ])
    expect(req.request.params.get('state')).toBe('rejected')
    expect(req.request.params.get('reviewer')).toBe('42')
    expect(req.request.params.get('page')).toBe('3')
    expect(req.request.params.get('page_size')).toBe('50')
    req.flush({ count: 0, results: [] })
  })

  it('omits empty state and reviewer filters', () => {
    service.queue('', null, 1, 20).subscribe()
    const req = http.expectOne((request) =>
      request.url.endsWith('review_queue/')
    )
    expect(req.request.params.keys().sort()).toEqual(['page', 'page_size'])
    req.flush({ count: 0, results: [] })
  })
})
