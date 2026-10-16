import { Dialog, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '@ury/ui'
import type { useOrderingSession } from '../hooks/useOrderingSession'
import { t } from '../i18n'

type Props = Pick<ReturnType<typeof useOrderingSession>, 'noteItem' | 'noteText' | 'changeNote' | 'confirmNote' | 'cancelNote'>

export default function GuestNoteDialog({ noteItem, noteText, changeNote, confirmNote, cancelNote }: Props) {
  return (
    <Dialog open={!!noteItem} onOpenChange={open => !open && cancelNote()}>
      <DialogContent onClose={cancelNote} closeLabel={t('dialog.close')}>
        <DialogHeader><DialogTitle>{noteItem?.item_name}</DialogTitle></DialogHeader>
        <div className="space-y-2 p-4">
          <label htmlFor="guest-item-note" className="block text-sm font-medium">{t('item.note_label')}</label>
          <textarea id="guest-item-note" maxLength={140} value={noteText}
            onChange={event => changeNote(event.target.value)} placeholder={t('item.note_placeholder')}
            className="min-h-24 w-full rounded-md border bg-background p-3 text-base" />
          <p className="text-right text-xs text-muted-foreground">{noteText.length}/140</p>
        </div>
        <DialogFooter>
          <button onClick={confirmNote} className="min-h-11 w-full rounded-md bg-primary px-4 py-3 font-medium text-primary-foreground">
            {t('item.add', { name: noteItem?.item_name ?? '' })}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
