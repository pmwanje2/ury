// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';

const loadDrawerKick = async () => import('./drawer-kick');

afterEach(() => {
  vi.restoreAllMocks();
});

describe('POS cash drawer kick event listener & config-driven auto-kick', () => {
  it('handles pos:drawer-kick window event via QZ or server endpoint without throwing on failure', async () => {
    const mod = await loadDrawerKick();
    const kickQz = vi.fn(async () => ({ status: 'Sent' as const, hex: '1b700119fa', printer: 'Till-1' }));
    const callServer = vi.fn(async () => ({ status: 'Sent' as const, transport: 'tcp' as const, hex: '1b700019fa' }));
    const results: unknown[] = [];

    const detach = mod.installDrawerKickListener({
      getProfile: () => ({
        pos_profile: 'Main Till',
        print_type: 'qz',
        qz_host: 'localhost',
        printer: 'Till-1',
        custom_drawer_pin: 2,
      }),
      onResult: (res) => results.push(res),
      deps: { kickQz, callServer },
    });

    try {
      // Exact event payload dispatched by #151 TillCashDialog.tsx:285
      window.dispatchEvent(new CustomEvent('pos:drawer-kick', { detail: { hex: '1b700119fa' } }));
      await vi.waitFor(() => expect(results).toHaveLength(1));
      expect(kickQz).toHaveBeenCalledWith('localhost', 'Till-1', '1b700119fa', 2);
      expect(results[0]).toMatchObject({ status: 'Sent', transport: 'qz', hex: '1b700119fa' });

      // Failed QZ kick reports Failed cleanly and never lies as Sent.
      kickQz.mockRejectedValueOnce(new Error('QZ offline'));
      window.dispatchEvent(new CustomEvent('pos:drawer-kick', { detail: { hex: '1b700019fa' } }));
      await vi.waitFor(() => expect(results).toHaveLength(2));
      expect(results[1]).toMatchObject({ status: 'Failed', transport: 'qz', hex: '1b700019fa' });
    } finally {
      detach();
    }
  });

  it('falls back to server kick_cash_drawer endpoint when terminal uses network printer', async () => {
    const mod = await loadDrawerKick();
    const callServer = vi.fn(async () => ({ status: 'Sent' as const, transport: 'cups' as const, hex: '1b700019fa' }));
    const res = await mod.executeDrawerKick(
      { posProfile: 'Bar Till', pin: 2, reason: 'No Sale' },
      {
        getProfile: () => ({ pos_profile: 'Bar Till', print_type: 'network', qz_host: null, printer: 'BAR-NET' }),
        callServer,
      },
    );
    expect(callServer).toHaveBeenCalledWith(
      expect.objectContaining({ pos_profile: 'Bar Till', drawer_pin: 2, hex_command: '1b700019fa', reason: 'No Sale' }),
    );
    expect(res).toMatchObject({ status: 'Sent', transport: 'cups', hex: '1b700019fa' });
  });

  it('auto-kicks strictly by Mode of Payment configuration, never hardcoded to "Cash"', async () => {
    const mod = await loadDrawerKick();

    // "Cash" NOT configured in drawerModes and open_cash_drawer=0 -> must NOT kick!
    expect(
      mod.shouldAutoKickForSettlement({
        autoKickEnabled: true,
        payments: [{ mode_of_payment: 'Cash', amount: 50000, open_cash_drawer: 0 }],
        drawerModes: [],
      }),
    ).toBe(false);

    // Custom tender configured in drawerModes -> MUST kick!
    expect(
      mod.shouldAutoKickForSettlement({
        autoKickEnabled: true,
        payments: [{ mode_of_payment: 'Register Notes', amount: 50000 }],
        drawerModes: ['Register Notes'],
      }),
    ).toBe(true);

    // Room Charge & Mobile Money with open_cash_drawer=0 -> must NOT kick!
    expect(
      mod.shouldAutoKickForSettlement({
        autoKickEnabled: true,
        payments: [
          { mode_of_payment: 'Room Charge', amount: 30000, open_cash_drawer: 0 },
          { mode_of_payment: 'Mobile Money', amount: 20000, open_cash_drawer: 0 },
        ],
        drawerModes: ['Register Notes'],
      }),
    ).toBe(false);

    // Profile auto-kick disabled -> must NOT kick even for drawer tender!
    expect(
      mod.shouldAutoKickForSettlement({
        autoKickEnabled: false,
        payments: [{ mode_of_payment: 'Register Notes', amount: 50000, open_cash_drawer: 1 }],
        drawerModes: ['Register Notes'],
      }),
    ).toBe(false);
  });

  it('forwards profile pin, custom hex, auto-kick flag and drawer_opening_modes from App profile wiring', async () => {
    const fs = await import('node:fs');
    const path = await import('node:path');
    const appSource = fs.readFileSync(path.resolve(__dirname, '../App.tsx'), 'utf8');
    expect(appSource).toContain('custom_drawer_pin');
    expect(appSource).toContain('custom_drawer_kick_hex');
    expect(appSource).toContain('custom_auto_kick_on_cash_tender');
    expect(appSource).toContain('drawer_opening_modes');

    const mod = await loadDrawerKick();
    const callServer = vi.fn(async () => ({ status: 'Sent' as const, transport: 'tcp' as const, hex: '1b700119fa' }));
    const results: unknown[] = [];

    const detach = mod.installDrawerKickListener({
      getProfile: () => ({
        pos_profile: 'R1 Till A',
        print_type: 'network',
        qz_host: null,
        printer: 'R1-NP-A-PRI',
        custom_drawer_pin: 5,
        custom_drawer_kick_hex: '1b700119fa',
        custom_auto_kick_on_cash_tender: 1,
        drawer_opening_modes: ['R1 Register Notes'],
      }),
      onResult: (res) => results.push(res),
      deps: { callServer },
    });

    try {
      // Unconfigured "Cash" tender does NOT kick.
      window.dispatchEvent(
        new CustomEvent('pos:payment-settled', {
          detail: { payments: [{ mode_of_payment: 'Cash', amount: 10000 }], posProfile: 'R1 Till A' },
        }),
      );
      await new Promise((r) => setTimeout(r, 20));
      expect(callServer).not.toHaveBeenCalled();

      // Configured drawer-opening tender auto-kicks using profile Pin 5 + custom hex.
      window.dispatchEvent(
        new CustomEvent('pos:payment-settled', {
          detail: { payments: [{ mode_of_payment: 'R1 Register Notes', amount: 10000 }], posProfile: 'R1 Till A' },
        }),
      );
      await vi.waitFor(() => expect(results).toHaveLength(1));
      expect(callServer).toHaveBeenCalledWith(
        expect.objectContaining({ pos_profile: 'R1 Till A', drawer_pin: 5, hex_command: '1b700119fa' }),
      );

      // pos:drawer-kick with empty detail also uses profile Pin 5 + custom hex (not Pin 2 fallback).
      window.dispatchEvent(new CustomEvent('pos:drawer-kick', { detail: { reason: 'No Sale' } }));
      await vi.waitFor(() => expect(results).toHaveLength(2));
      expect(callServer).toHaveBeenLastCalledWith(
        expect.objectContaining({ pos_profile: 'R1 Till A', drawer_pin: 5, hex_command: '1b700119fa', reason: 'No Sale' }),
      );
    } finally {
      detach();
    }
  });
});
