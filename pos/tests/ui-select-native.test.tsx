import { act } from 'react';
import { createRoot } from 'react-dom/client';
import { afterEach, expect, it, vi } from 'vitest';
import { Select, SelectItem, RadixSelect } from '@ury/ui';

// The shared @ury/ui Select stays a native <select> (as on ury-erp/ury
// version-16): coded void reasons, till profiles, aggregators and report
// settings rely on native option semantics. Upstream version-2 #358 swapped
// it for a Radix popover; its own Reservations status filter uses
// RadixSelect directly, which stays exported.
afterEach(() => {
  document.body.replaceChildren();
  vi.unstubAllGlobals();
});

it('renders a native select whose options carry their values', async () => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
  const onValueChange = vi.fn();
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => root.render(
    <Select value="" onValueChange={onValueChange} placeholder="Choose">
      <SelectItem value="Kitchen error">Kitchen error</SelectItem>
      <SelectItem value="__other__">Other</SelectItem>
    </Select>,
  ));
  const select = container.querySelector('select');
  expect(select).not.toBeNull();
  expect([...select!.querySelectorAll('option')].map(o => o.value).filter(Boolean)).toEqual(['Kitchen error', '__other__']);
  await act(async () => {
    select!.value = 'Kitchen error';
    select!.dispatchEvent(new Event('change', { bubbles: true }));
  });
  expect(onValueChange).toHaveBeenCalledWith('Kitchen error');
  await act(async () => root.unmount());
});

it('keeps RadixSelect available for the upstream reservation status filter', () => {
  expect(RadixSelect.Root).toBeDefined();
});
