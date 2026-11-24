import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ProductConfigurator } from './product-configurator';

const BASE_LABELS = {
  specialInstructions: 'Special Instructions',
  specialInstructionsPlaceholder: 'Add notes...',
  quantity: 'Quantity',
  variants: 'Variants',
  addons: 'Add-ons',
  loadingAddons: 'Loading...',
  noAddons: 'No add-ons',
  total: 'Total',
  submit: 'Add to Order',
  requiredBadge: 'Required',
  optionalBadge: 'Optional',
  includedBadge: 'Included',
  soldOutBadge: 'Sold Out',
  regularPrefix: 'Regular',
  prefixStripLabel: 'Modifier prefix',
  selectRequiredPrompt: 'Complete required selections',
};

describe('ProductConfigurator modifier & combo groups', () => {
  it('renders prefix touch strip (>=44px), required counters, Included badge, and disables sold-out options', async () => {
    const user = userEvent.setup();
    const onToggleOption = vi.fn();
    const onSelectPrefix = vi.fn();

    render(
      <ProductConfigurator
        open
        onOpenChange={vi.fn()}
        itemName="Ribeye Steak"
        itemCode="RIBEYE"
        quantity="1"
        onQuantityChange={vi.fn()}
        onIncrement={vi.fn()}
        onDecrement={vi.fn()}
        comments=""
        onCommentsChange={vi.fn()}
        variants={[]}
        onSelectVariant={vi.fn()}
        addons={[]}
        selectedAddonIds={[]}
        onToggleAddon={vi.fn()}
        totalLabel="UGX 65,000"
        submitDisabled
        onSubmit={vi.fn()}
        labels={BASE_LABELS}
        modifierGroups={[
          {
            name: 'Meat Temperature',
            displayTitle: 'Meat Temperature',
            groupType: 'Condiment',
            required: true,
            minSelections: 1,
            maxSelections: 1,
            allowPrefixes: false,
            allowedPrefixes: [],
            selectedCount: 0,
            options: [
              {
                optionCode: 'MED-RARE',
                label: 'Medium Rare',
                priceLabel: null,
                selected: false,
                isDefault: false,
                available: true,
              },
            ],
          },
          {
            name: 'Steak Extras',
            displayTitle: 'Steak Extras',
            groupType: 'Condiment',
            required: false,
            minSelections: 0,
            maxSelections: 2,
            allowPrefixes: true,
            allowedPrefixes: ['No', 'Extra', 'Side', 'Light', 'On Side'],
            activePrefix: 'No',
            selectedCount: 1,
            options: [
              {
                optionCode: 'ONION',
                label: 'Onion',
                prefix: 'No',
                priceLabel: null,
                selected: true,
                isDefault: false,
                available: true,
              },
              {
                optionCode: 'EXTRA-CHEESE',
                label: 'Aged Cheddar',
                priceLabel: '+UGX 3,000',
                selected: false,
                isDefault: false,
                available: false,
              },
            ],
          },
          {
            name: 'Choose Side',
            displayTitle: 'Choose Side',
            groupType: 'Combo Choice',
            required: true,
            minSelections: 1,
            maxSelections: 1,
            allowPrefixes: false,
            allowedPrefixes: [],
            selectedCount: 1,
            options: [
              {
                optionCode: 'FRIES',
                label: 'French Fries',
                priceLabel: null,
                selected: true,
                isDefault: true,
                available: true,
              },
            ],
          },
        ]}
        onSelectModifierPrefix={onSelectPrefix}
        onToggleModifierOption={onToggleOption}
        missingRequiredGroups={['Meat Temperature']}
      />
    );

    // Prefix strip renders touch targets >= 44px (min-h-[44px])
    const noBtn = screen.getByRole('button', { name: 'No' });
    expect(noBtn.className).toContain('min-h-[44px]');
    await user.click(screen.getByRole('button', { name: 'Extra' }));
    expect(onSelectPrefix).toHaveBeenCalledWith('Steak Extras', 'Extra');

    // Required group option tap triggers callback
    const medRareBtn = screen.getByRole('button', { name: /Medium Rare/i });
    expect(medRareBtn.className).toContain('min-h-[44px]');
    await user.click(medRareBtn);
    expect(onToggleOption).toHaveBeenCalledWith('Meat Temperature', 'MED-RARE');

    // Sold-out option is disabled
    const cheeseBtn = screen.getByRole('button', { name: /Aged Cheddar/i });
    expect(cheeseBtn).toBeDisabled();

    // Included badge shown on default combo option
    expect(screen.getByText('Included')).toBeInTheDocument();

    // Missing required prompt shown & submit disabled
    expect(screen.getByText(/Complete required selections/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Add to Order' })).toBeDisabled();
  });
});
