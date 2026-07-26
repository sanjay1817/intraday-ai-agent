import { interval, Observable, startWith, switchMap } from 'rxjs';

// Every dashboard page polls its own data on this same cadence (Phase 4
// requirement: "Auto-refresh every 2 seconds").
export const POLL_INTERVAL_MS = 2000;

// Shared by every page component instead of each re-implementing its
// own interval/switchMap/startWith pipeline -- fires immediately, then
// every `POLL_INTERVAL_MS`, always switching to the latest in-flight
// request rather than piling requests up if one is slow.
export function pollEvery<T>(source: () => Observable<T>): Observable<T> {
  return interval(POLL_INTERVAL_MS).pipe(
    startWith(0),
    switchMap(() => source())
  );
}
