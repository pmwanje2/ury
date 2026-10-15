import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@ury/ui'
import { t } from '../i18n'
import { GUEST_NOTE_LIMIT, type GuestMenuItem } from '../lib/guest-lines'

interface GuestNoteDialogProps {
  item: GuestMenuItem | null
  text: string
  onText: (value: string) => void
  onConfirm: () => void
  onClose: () => void
}

function GuestNoteDialog({ item, text, onText, onConfirm, onClose }: GuestNoteDialogProps) {
  return (
    <Dialog open={item != null} onOpenChange={(open) => { if (!open) onClose() }}>
      <DialogContent onClose={onClose} closeLabel={t('dialog.close')}>
        <DialogHeader>
          <DialogTitle>{item?.item_name}</DialogTitle>
        </DialogHeader>
        <label className="block text-sm">
          <span>{t('item.note_label')}</span>
          <textarea
            className="mt-2 w-full rounded-md border p-2"
            maxLength={GUEST_NOTE_LIMIT}
            value={text}
            placeholder={t('item.note_placeholder')}
            aria-label={t('item.note_label')}
            onChange={(event) => onText(event.target.value.slice(0, GUEST_NOTE_LIMIT))}
          />
        </label>
        <DialogFooter>
          <button
            type="button"
            onClick={onConfirm}
            disabled={item == null}
            className="mt-3 w-full rounded-md bg-primary py-3 font-medium text-primary-foreground disabled:opacity-50"
          >
            {t('item.add', { name: item?.item_name ?? '' })}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default GuestNoteDialog
