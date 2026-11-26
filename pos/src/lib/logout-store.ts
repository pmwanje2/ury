// Independent of the offline order database so that a queue purge cannot clear this marker.
const STORE = 'logout';

async function markerRequest<T>(mode: IDBTransactionMode, operation: (store: IDBObjectStore) => IDBRequest<T>): Promise<T> {
  const db = await new Promise<IDBDatabase>((resolve, reject) => {
    const open = indexedDB.open('ury-pos-session', 1);
    open.onupgradeneeded = () => open.result.createObjectStore(STORE);
    open.onerror = () => reject(open.error);
    open.onsuccess = () => resolve(open.result);
  });
  try {
    return await new Promise<T>((resolve, reject) => {
      const transaction = db.transaction(STORE, mode);
      transaction.onabort = () => reject(transaction.error || new Error('Logout marker transaction aborted'));
      const request = operation(transaction.objectStore(STORE));
      transaction.oncomplete = () => resolve(request.result);
    });
  } finally {
    db.close();
  }
}

export async function readLogoutMarker(): Promise<boolean> {
  return Boolean(await markerRequest('readonly', store => store.get('failed')));
}

export async function writeLogoutMarker(user: string | null): Promise<void> {
  if (user === null) await markerRequest('readwrite', store => store.delete('failed'));
  else await markerRequest('readwrite', store => store.put(user, 'failed'));
}

export async function readGeneration(): Promise<string | undefined> {
  return markerRequest('readonly', store => store.get('generation'));
}

export async function writeGeneration(generation: string): Promise<void> {
  await markerRequest('readwrite', store => store.put(generation, 'generation'));
}
