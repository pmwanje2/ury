import { io } from 'socket.io-client'

export function subscribeMenuAvailability(branch: string, refresh: () => void) {
  const controller = new AbortController()
  const channel = `menu_availability_update_${branch}`
  let socket: ReturnType<typeof io> | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  const update = () => {
    clearTimeout(timer)
    timer = setTimeout(refresh, 500)
  }
  void fetch('/api/method/ury.ury.api.ury_kot_display.get_site_name', { signal: controller.signal })
    .then(response => response.json())
    .then(data => {
      const site = data.message?.site_name
      if (site && !controller.signal.aborted) {
        socket = io(`${window.location.origin}/${site}`, { withCredentials: true })
        socket.on(channel, update)
      }
    }).catch(() => { /* Periodic refresh remains available without realtime. */ })
  return () => {
    controller.abort()
    clearTimeout(timer)
    socket?.off(channel, update)
    socket?.disconnect()
  }
}
