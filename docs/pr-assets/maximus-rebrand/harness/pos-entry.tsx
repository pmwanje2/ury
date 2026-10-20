// Mounts the real POS components. The full /pos app needs a POS Profile and
// opening entry, so these two screens are mounted directly (disclosed in README).
import { createRoot } from 'react-dom/client';
import { initI18n } from '/src/i18n';
import InitialLoader from '/src/components/InitialLoader';
import ScreenSizeProvider from '/src/components/ScreenSizeProvider';
import '/src/index.css';

const screen = new URLSearchParams(window.location.search).get('screen');

initI18n('en').then(() => {
  createRoot(document.getElementById('root')!).render(
    screen === 'gate'
      ? <ScreenSizeProvider><div>till</div></ScreenSizeProvider>
      : <InitialLoader />,
  );
});
