import type { Control } from './types';

const dictionary = new Intl.Collator('zh-CN', { numeric: false });

/** Display order is independent of detection order and each team's route. */
export function compareControls(a: Pick<Control, 'label' | 'id'>, b: Pick<Control, 'label' | 'id'>) {
  return dictionary.compare(a.label, b.label) || dictionary.compare(a.id, b.id);
}

export function sortedControls<T extends Pick<Control, 'label' | 'id'>>(controls: T[]): T[] {
  return [...controls].sort(compareControls);
}
