import React from 'react';
import { createRoot } from 'react-dom/client';
import { MemoryRouter } from 'react-router-dom';
import FloorPinPad from '/src/components/FloorPinPad';
import CaptainTables from '/src/captain/pages/CaptainTables';
import { initI18n } from '/src/i18n';
import './visual.css';

const params = new URLSearchParams(window.location.search);
const screen = params.get('screen') || 'pad';
const language = params.get('lang') || 'en';
document.body.dataset.screen = screen;

await initI18n(language);
document.documentElement.lang = language;
document.documentElement.dir = language === 'ar' ? 'rtl' : 'ltr';

const root = createRoot(document.getElementById('root')!);
if (screen === 'captain') {
  root.render(
    <MemoryRouter initialEntries={['/order']}>
      <CaptainTables />
    </MemoryRouter>,
  );
} else {
  root.render(<FloorPinPad offline={screen === 'offline'} />);
}
