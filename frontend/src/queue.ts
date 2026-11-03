export function waitLabel(seconds: number | null): string {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return 'Оцінка поки недоступна';
  if (seconds < 1) return 'менше секунди';
  if (seconds < 60) return `близько ${Math.ceil(seconds)} с`;
  return `близько ${Math.ceil(seconds / 60)} хв`;
}
