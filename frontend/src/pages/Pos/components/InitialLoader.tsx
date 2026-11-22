import { resolveBrand } from '@ury/core';
import React from 'react';
import { Spinner } from '@ury/ui';
import { t } from '../i18n';

const InitialLoader: React.FC = () => {
  const brand = resolveBrand({ name: 'Maximus POS' });
  return (
    <div className="fixed inset-0 bg-background flex items-center justify-center">
      <div className="text-center">
        <Spinner className="w-12 h-12"  message={t('common.loading')} />
        <p className="mt-4 text-lg font-medium text-foreground">{t('common.loading_ury_pos', { name: brand.name })}</p>
        <p className="mt-2 text-sm text-text-tertiary">{t('common.please_wait_setup')}</p>
      </div>
    </div>
  );
};

export default InitialLoader; 