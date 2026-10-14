// jsdom has no Web Locks; mirror exclusive queuing for real IndexedDB tests.
export function testLocks() {
  let tail: Promise<unknown> = Promise.resolve();
  return { request: <T,>(_name: string, run: () => Promise<T>) => {
    const next = tail.then(run); tail = next.catch(() => undefined); return next;
  } };
}
