import { HttpClient, HttpParams } from '@angular/common/http'
import { Injectable, inject } from '@angular/core'
import { Observable } from 'rxjs'
import {
  DocumentReview,
  ReviewMutation,
  ReviewQueue,
  ReviewState,
} from 'src/app/data/document-review'
import { environment } from 'src/environments/environment'

@Injectable({ providedIn: 'root' })
export class DocumentReviewService {
  private http = inject(HttpClient)

  get(documentId: number): Observable<DocumentReview> {
    return this.http.get<DocumentReview>(this.url(documentId))
  }

  mutate(
    documentId: number,
    mutation: ReviewMutation
  ): Observable<DocumentReview> {
    return this.http.post<DocumentReview>(this.url(documentId), mutation)
  }

  queue(
    state: ReviewState | '',
    reviewer: number | null,
    page: number,
    pageSize: number
  ): Observable<ReviewQueue> {
    let params = new HttpParams().set('page', page).set('page_size', pageSize)
    if (state) params = params.set('state', state)
    if (reviewer != null) params = params.set('reviewer', reviewer)
    return this.http.get<ReviewQueue>(
      `${environment.apiBaseUrl}review_queue/`,
      {
        params,
      }
    )
  }

  private url(documentId: number): string {
    return `${environment.apiBaseUrl}documents/${documentId}/review/`
  }
}
