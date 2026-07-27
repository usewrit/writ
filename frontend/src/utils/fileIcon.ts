import type { ElementType } from 'react';
import {
  PhotoIcon,
  FilmIcon,
  MusicalNoteIcon,
  TableCellsIcon,
  DocumentTextIcon,
  ArchiveBoxIcon,
  CodeBracketIcon,
  DocumentIcon,
} from '@heroicons/react/24/outline';

/**
 * A type-bucketed heroicon for a stored file, chosen by content-type with an
 * extension fallback. Pure (no JSX) so the file-library page and the FilePicker
 * render the same affordance for the same file. Returns the icon COMPONENT —
 * the caller renders it (`const Icon = fileTypeIcon(...); <Icon .../>`).
 */
export function fileTypeIcon(
  contentType?: string | null,
  filename?: string | null,
): ElementType {
  const ct = (contentType || '').toLowerCase();
  const name = (filename || '').toLowerCase();
  if (ct.startsWith('image/')) return PhotoIcon;
  if (ct.startsWith('video/')) return FilmIcon;
  if (ct.startsWith('audio/')) return MusicalNoteIcon;
  if (ct === 'application/pdf' || name.endsWith('.pdf')) return DocumentTextIcon;
  if (
    ct.includes('csv') ||
    ct.includes('spreadsheet') ||
    ct.includes('excel') ||
    name.endsWith('.csv') ||
    name.endsWith('.xlsx') ||
    name.endsWith('.xls')
  )
    return TableCellsIcon;
  if (
    ct.includes('zip') ||
    ct.includes('tar') ||
    ct.includes('gzip') ||
    ct.includes('compressed') ||
    name.endsWith('.zip') ||
    name.endsWith('.gz') ||
    name.endsWith('.tar')
  )
    return ArchiveBoxIcon;
  if (
    ct.includes('json') ||
    ct.includes('javascript') ||
    ct.includes('xml') ||
    ct.startsWith('text/') ||
    name.endsWith('.json') ||
    name.endsWith('.js') ||
    name.endsWith('.ts') ||
    name.endsWith('.html')
  )
    return CodeBracketIcon;
  return DocumentIcon;
}
