import { logout as endSession } from '../../packages/core/src/logout.js';

// Mosaic still writes unnamespaced ticket/row strike markers.
export const logout = () => endSession({ includeMosaicStrikes: true });
