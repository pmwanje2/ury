export function watchIdle(onIdle: () => void, ms: number): () => void {
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
    fired = false;
    deadline = Date.now() + ms;
    arm();
  };

  const checkDeadline = () => {
    if (Date.now() >= deadline) fire();
  };

  const stop = () => {
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
