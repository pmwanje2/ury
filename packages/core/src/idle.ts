/**
 * Call `onIdle` once after `ms` of no pointer or key activity. The deadline is
 * wall-clock based, so a tablet that slept past it fires on wake (focus or
 * visibilitychange) instead of one full period later. `ms <= 0` disables it.
 * An optional cleanup cancels deferred idle work on activity or stop.
 * Returns a function that removes every listener and timer.
 */
export function watchIdle(onIdle: () => void, ms: number, onIdleEnd?: () => void): () => void {
  if (ms <= 0) return () => {};

  let deadline = Date.now() + ms;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let fired = false;

  const clearTimer = () => {
    if (timer !== undefined) clearTimeout(timer);
    timer = undefined;
  };

  const fire = () => {
    if (fired) return;
    fired = true;
    clearTimer();
    onIdle();
  };

  const arm = () => {
    clearTimer();
    timer = setTimeout(fire, Math.max(0, deadline - Date.now()));
  };

  const reset = () => {
    if (fired) onIdleEnd?.();
    fired = false;
    deadline = Date.now() + ms;
    arm();
  };

  const checkDeadline = () => {
    if (Date.now() >= deadline) fire();
  };

  const stop = () => {
    if (fired) onIdleEnd?.();
    clearTimer();
    window.removeEventListener('pointerdown', reset);
    window.removeEventListener('keydown', reset);
    window.removeEventListener('focus', checkDeadline);
    document.removeEventListener('visibilitychange', checkDeadline);
  };

  window.addEventListener('pointerdown', reset);
  window.addEventListener('keydown', reset);
  window.addEventListener('focus', checkDeadline);
  document.addEventListener('visibilitychange', checkDeadline);
  arm();

  return stop;
}
