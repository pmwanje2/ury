import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, createElement, useState } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import * as core from '@ury/core';
import { useIdleReset } from '../../self-order/src/hooks/useIdleReset';

let root: Root | undefined;
let host: HTMLDivElement | undefined;
let stops: Array<() => void>;

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-10-14T12:00:00Z'));
  stops = [];
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

afterEach(() => {
  if (root) act(() => root!.unmount());
  host?.remove();
  root = undefined;
  host = undefined;
  stops.forEach(stop => stop());
  vi.useRealTimers();
  vi.restoreAllMocks();
});

function watch(onIdle: () => void, ms: number) {
  // Missing public behavior is an assertion failure, not a module-load error.
  expect(core.watchIdle).toBeTypeOf('function');
  const stop = core.watchIdle(onIdle, ms);
  stops.push(stop);
  return stop;
}

describe('shared wall-clock idle watcher', () => {
  it('fires once at the deadline, not before or again on wake', () => {
    const onIdle = vi.fn();
    watch(onIdle, 60000);
    vi.advanceTimersByTime(59999);
    expect(onIdle).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    document.dispatchEvent(new Event('visibilitychange'));
    window.dispatchEvent(new Event('focus'));
    vi.advanceTimersByTime(120000);
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it.each(['pointerdown', 'keydown'])('%s resets the deadline', (event) => {
    const onIdle = vi.fn();
    watch(onIdle, 60000);
    vi.advanceTimersByTime(45000);
    window.dispatchEvent(new Event(event));
    vi.advanceTimersByTime(59999);
    expect(onIdle).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it.each([0, -1])('%s disables timers and listeners', (ms) => {
    const onIdle = vi.fn();
    const win = vi.spyOn(window, 'addEventListener');
    const doc = vi.spyOn(document, 'addEventListener');
    watch(onIdle, ms);
    vi.advanceTimersByTime(120000);
    window.dispatchEvent(new Event('focus'));
    expect(onIdle).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
    expect(win).not.toHaveBeenCalled();
    expect(doc).not.toHaveBeenCalled();
  });

  it.each(['visibilitychange', 'focus'])('%s expires immediately after sleeping past the deadline', (event) => {
    const onIdle = vi.fn();
    watch(onIdle, 60000);
    // Jump the wall clock without delivering the suspended timeout.
    vi.setSystemTime(new Date('2026-10-14T12:02:00Z'));
    (event === 'visibilitychange' ? document : window).dispatchEvent(new Event(event));
    expect(onIdle).toHaveBeenCalledTimes(1);
    vi.advanceTimersByTime(120000);
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it('a wake before the deadline does not extend it', () => {
    const onIdle = vi.fn();
    watch(onIdle, 60000);
    vi.advanceTimersByTime(45000);
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
    vi.advanceTimersByTime(15000);
    expect(onIdle).toHaveBeenCalledTimes(1);
  });

  it('stop removes every listener and cancels the pending timeout', () => {
    const winAdd = vi.spyOn(window, 'addEventListener');
    const docAdd = vi.spyOn(document, 'addEventListener');
    const winRemove = vi.spyOn(window, 'removeEventListener');
    const docRemove = vi.spyOn(document, 'removeEventListener');
    const onIdle = vi.fn();
    const stop = watch(onIdle, 60000);
    expect(winAdd.mock.calls.map(([event]) => event)).toEqual(['pointerdown', 'keydown', 'focus']);
    expect(docAdd.mock.calls.map(([event]) => event)).toEqual(['visibilitychange']);
    stop();
    for (const [event, listener] of winAdd.mock.calls) expect(winRemove).toHaveBeenCalledWith(event, listener);
    for (const [event, listener] of docAdd.mock.calls) expect(docRemove).toHaveBeenCalledWith(event, listener);
    vi.advanceTimersByTime(120000);
    window.dispatchEvent(new Event('pointerdown'));
    window.dispatchEvent(new Event('focus'));
    document.dispatchEvent(new Event('visibilitychange'));
    vi.advanceTimersByTime(120000);
    expect(onIdle).not.toHaveBeenCalled();
    expect(vi.getTimerCount()).toBe(0);
  });

  it('new activity rearms a kiosk after its previous idle reset', () => {
    const onIdle = vi.fn();
    watch(onIdle, 60000);
    vi.advanceTimersByTime(60000);
    window.dispatchEvent(new Event('pointerdown'));
    vi.advanceTimersByTime(60000);
    expect(onIdle).toHaveBeenCalledTimes(2);
  });
});

it('the React wrapper resets the kiosk cart immediately on wake and cleans up', () => {
  function Kiosk() {
    const [cart, setCart] = useState('Coffee');
    useIdleReset(() => setCart('Empty'), 60000);
    return createElement('div', null, cart);
  }
  host = document.createElement('div');
  document.body.append(host);
  root = createRoot(host);
  act(() => root!.render(createElement(Kiosk)));
  expect(host.textContent).toBe('Coffee');
  vi.setSystemTime(new Date('2026-10-14T12:02:00Z'));
  act(() => document.dispatchEvent(new Event('visibilitychange')));
  expect(host.textContent).toBe('Empty');
  act(() => root!.unmount());
  root = undefined;
  expect(vi.getTimerCount()).toBe(0);
});
