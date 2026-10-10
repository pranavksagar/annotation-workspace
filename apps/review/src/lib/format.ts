import type { AnnotationStatus } from './annotationTypes';

export const STATUS_LABEL: Record<AnnotationStatus, string> = {TO_DO: 'TO DO', IN_PROGRESS: 'IN PROGRESS', COMPLETED: 'COMPLETED'};

const rtf = typeof Intl !== 'undefined' ? new Intl.RelativeTimeFormat(undefined, {numeric: 'auto'}) : null;

export function relativeTime(iso: string, now = Date.now()): string {
  const seconds = (new Date(iso).getTime() - now) / 1000;
  const abs = Math.abs(seconds);
  if (!rtf) return new Date(iso).toLocaleString();
  if (abs < 45) return 'just now';
  if (abs < 3600) return rtf.format(Math.round(seconds / 60), 'minute');
  if (abs < 86400) return rtf.format(Math.round(seconds / 3600), 'hour');
  if (abs < 86400 * 30) return rtf.format(Math.round(seconds / 86400), 'day');
  return new Date(iso).toLocaleDateString();
}
