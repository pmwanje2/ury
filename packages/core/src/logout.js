import { postLogout } from './frappe/floor-request';
import { clearAppStorage, purgeOfflineSession } from './storage';

export async function logout({ includeMosaicStrikes = false, floorUser } = {}) {
  await postLogout(floorUser);
  clearAppStorage({ includeMosaicStrikes });
  await purgeOfflineSession();
}
