export const layoutFor = (variant: 'vertical' | 'landscape') => {
  const vertical = variant === 'vertical';
  return {
    vertical,
    paddingX: vertical ? 72 : 120,
    paddingTop: vertical ? 150 : 96,
    paddingBottom: vertical ? 340 : 160,
    titleSize: vertical ? 82 : 76,
    bodySize: vertical ? 42 : 34,
    cardGap: vertical ? 28 : 34,
    radius: vertical ? 34 : 28,
  };
};

export const palette = {
  background: '#F4EDE4',
  paper: '#FFFDFC',
  ink: '#29332E',
  muted: '#748078',
  sage: '#9EA9A1',
  clay: '#C9A58C',
  accent: '#677A70',
  positive: '#6F8B77',
  negative: '#A67C6D',
  code: '#24302B',
};

