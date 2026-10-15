import { formatCurrency } from '@ury/core'
import { t } from '../../i18n'
import type { AddonChoice } from '../../lib/guest-lines'

interface AddonChoicesProps {
  choices: AddonChoice[]
  onAdd: (choice: AddonChoice) => void
}

function AddonChoices({ choices, onAdd }: AddonChoicesProps) {
  if (choices.length === 0) return null
  return (
    <ul aria-label={t('item.addons')} className="mt-2 space-y-1">
      {choices.map((choice) => (
        <li key={choice.item_code}>
          <button
            type="button"
            onClick={() => onAdd(choice)}
            aria-label={t('item.addon_add', { name: choice.item_name })}
            className="flex w-full items-center justify-between gap-2 rounded-md border px-2 py-1 text-left text-sm"
          >
            <span>{choice.item_name}</span>
            {choice.rate != null && <span className="tabular-nums">{formatCurrency(choice.rate)}</span>}
          </button>
        </li>
      ))}
    </ul>
  )
}

export default AddonChoices
