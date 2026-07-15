import React from 'react';
import {Img, interpolate, OffthreadVideo, staticFile, useCurrentFrame} from 'remotion';
import {layoutFor, palette} from '../layout';
import {SceneComponentProps} from '../types';
import {asString, asStrings, Card, SceneFrame, Title} from './common';

export const HookOutcome: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow={asString(props.eyebrow, 'LECTURECAST · OUTCOME')}>
    <Title variant={variant}>{asString(props.headline)}</Title>
    <div style={{display: 'grid', gridTemplateColumns: variant === 'vertical' ? '1fr' : '1fr 1fr', gap: 28, marginTop: 54}}>
      <Card tone="clay"><div style={{fontSize: 24, color: palette.negative, fontWeight: 800}}>BEFORE</div><div style={{fontSize: 38, marginTop: 12, fontWeight: 750}}>{asString(props.before)}</div></Card>
      <Card tone="sage"><div style={{fontSize: 24, color: palette.positive, fontWeight: 800}}>AFTER</div><div style={{fontSize: 38, marginTop: 12, fontWeight: 750}}>{asString(props.after)}</div></Card>
    </div>
  </SceneFrame>
);

export const HookQuestion: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow="QUESTION">
    <Title variant={variant}>{asString(props.question)}</Title>
    <Card tone="sage" style={{marginTop: 54, fontSize: 38, fontWeight: 750}}>{asString(props.promise)}</Card>
  </SceneFrame>
);

export const SectionTitle: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow={asString(props.kicker, 'NEXT')}>
    <Title variant={variant}>{asString(props.title)}</Title>
    {props.subtitle ? <div style={{fontSize: layoutFor(variant).bodySize, color: palette.muted, marginTop: 30}}>{asString(props.subtitle)}</div> : null}
  </SceneFrame>
);

export const ConceptCards: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow="CONCEPTS">
    <Title variant={variant}>{asString(props.title)}</Title>
    <div style={{display: 'grid', gridTemplateColumns: variant === 'vertical' ? '1fr 1fr' : `repeat(${Math.min(4, asStrings(props.cards).length)}, 1fr)`, gap: 24, marginTop: 48}}>
      {asStrings(props.cards).map((card, index) => <Card key={card} tone={index % 2 ? 'sage' : 'paper'}><div style={{fontSize: 24, color: palette.accent, fontWeight: 900}}>0{index + 1}</div><div style={{fontSize: 34, fontWeight: 800, marginTop: 20}}>{card}</div></Card>)}
    </div>
  </SceneFrame>
);

export const BeforeAfter: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow="BEFORE / AFTER">
    <Title variant={variant}>{asString(props.title)}</Title>
    <div style={{display: 'grid', gridTemplateColumns: variant === 'vertical' ? '1fr' : '1fr 1fr', gap: 28, marginTop: 48}}>
      <Card tone="clay"><div style={{fontSize: 30, fontWeight: 900, color: palette.negative}}>过去</div><div style={{fontSize: 36, marginTop: 20}}>{asString(props.before)}</div></Card>
      <Card tone="sage"><div style={{fontSize: 30, fontWeight: 900, color: palette.positive}}>现在</div><div style={{fontSize: 36, marginTop: 20}}>{asString(props.after)}</div></Card>
    </div>
  </SceneFrame>
);

export const ProcessFlow: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow="PROCESS">
    <Title variant={variant}>{asString(props.title)}</Title>
    <div style={{display: 'flex', flexDirection: variant === 'vertical' ? 'column' : 'row', gap: 20, marginTop: 50, alignItems: 'stretch'}}>
      {asStrings(props.steps).map((step, index, steps) => <React.Fragment key={step}><Card tone={index % 2 ? 'sage' : 'paper'} style={{flex: 1, fontSize: 30, fontWeight: 800}}><span style={{color: palette.accent, marginRight: 12}}>{index + 1}.</span>{step}</Card>{index < steps.length - 1 ? <div style={{alignSelf: 'center', fontSize: 34, color: palette.accent}}>{variant === 'vertical' ? '↓' : '→'}</div> : null}</React.Fragment>)}
    </div>
  </SceneFrame>
);

export const StepFocus: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow={`STEP ${String(props.step_number ?? 1).padStart(2, '0')}`}>
    <Title variant={variant}>{asString(props.title)}</Title>
    <Card tone="sage" style={{marginTop: 52, fontSize: layoutFor(variant).bodySize, lineHeight: 1.5}}>{asString(props.instruction)}</Card>
  </SceneFrame>
);

export const ProductUIFocus: React.FC<SceneComponentProps> = ({scene, props, variant}) => {
  const frame = useCurrentFrame();
  const steps = asStrings(props.steps);
  const active = Math.min(steps.length - 1, Math.floor(frame / 45));
  const asset = scene.assets[0];
  const assetSrc = asset ? (asset.uri.startsWith('https://') ? asset.uri : staticFile(asset.uri)) : null;
  return (
    <SceneFrame variant={variant} eyebrow="PRODUCT DEMO">
      <Title variant={variant}>{asString(props.title)}</Title>
      <div style={{display: 'grid', gridTemplateColumns: variant === 'vertical' ? '1fr' : '0.85fr 1.5fr', gap: 28, marginTop: 40}}>
        <div style={{display: 'flex', flexDirection: 'column', gap: 16}}>{steps.map((step, index) => <div key={step} style={{padding: '22px 26px', borderRadius: 22, fontSize: 28, fontWeight: 800, background: index === active ? palette.accent : palette.paper, color: index === active ? 'white' : palette.ink, border: `2px solid ${palette.sage}`}}>{index + 1}. {step}</div>)}</div>
        <Card style={{minHeight: variant === 'vertical' ? 390 : 330, padding: 0, overflow: 'hidden', position: 'relative'}}>
          {assetSrc && asset?.media_type === 'video' ? <OffthreadVideo src={assetSrc} muted style={{width: '100%', height: '100%', objectFit: 'cover', position: 'absolute'}} /> : null}
          {assetSrc && asset?.media_type === 'image' ? <Img src={assetSrc} style={{width: '100%', height: '100%', objectFit: 'cover', position: 'absolute'}} /> : null}
          {!assetSrc ? <><div style={{height: 42, background: '#E7E1D9', display: 'flex', gap: 9, alignItems: 'center', padding: '0 18px'}}>{['#A67C6D', '#C9A58C', '#9EA9A1'].map((color) => <div key={color} style={{width: 12, height: 12, borderRadius: 6, background: color}} />)}</div><div style={{padding: 30, display: 'grid', gridTemplateColumns: '0.4fr 1fr', gap: 22}}><div style={{background: '#E5EAE6', borderRadius: 16}} /><div>{[0, 1, 2, 3].map((row) => <div key={row} style={{height: 34, width: `${92 - row * 9}%`, background: row === active ? '#B8C9BE' : '#ECE8E2', borderRadius: 10, marginBottom: 18}} />)}</div></div></> : null}
        </Card>
      </div>
    </SceneFrame>
  );
};

export const MetricChange: React.FC<SceneComponentProps> = ({props, variant}) => {
  const frame = useCurrentFrame();
  const progress = interpolate(frame, [0, 30], [0, 1], {extrapolateRight: 'clamp'});
  return (
    <SceneFrame variant={variant} eyebrow="METRIC">
      <Title variant={variant}>{asString(props.label)}</Title>
      <div style={{display: 'flex', alignItems: 'baseline', gap: 34, marginTop: 70}}><span style={{fontSize: variant === 'vertical' ? 92 : 84, color: palette.muted, textDecoration: 'line-through'}}>{asString(props.before)}</span><span style={{fontSize: 54, color: palette.accent}}>→</span><span style={{fontSize: variant === 'vertical' ? 180 : 150, fontWeight: 950, color: palette.positive, transform: `scale(${0.85 + progress * 0.15})`}}>{asString(props.after)}</span><span style={{fontSize: 32, color: palette.muted}}>{asString(props.unit)}</span></div>
    </SceneFrame>
  );
};

export const CodeWalkthrough: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow={asString(props.language, 'CODE').toUpperCase()}>
    <Title variant={variant}>{asString(props.title)}</Title>
    <div style={{marginTop: 40, background: palette.code, color: '#E7EFEA', borderRadius: 28, padding: 36, fontFamily: 'SFMono-Regular, Menlo, monospace', fontSize: variant === 'vertical' ? 26 : 28, lineHeight: 1.55}}>{asStrings(props.code_lines).map((line, index) => <div key={`${index}-${line}`}><span style={{color: '#819087', marginRight: 22}}>{String(index + 1).padStart(2, '0')}</span>{line}</div>)}</div>
  </SceneFrame>
);

export const EndingSummary: React.FC<SceneComponentProps> = ({props, variant}) => (
  <SceneFrame variant={variant} eyebrow="SUMMARY">
    <Title variant={variant}>{asString(props.summary)}</Title>
    <div style={{marginTop: 58, display: 'inline-flex', alignSelf: 'flex-start', background: palette.accent, color: 'white', borderRadius: 999, padding: '20px 34px', fontSize: 30, fontWeight: 850}}>{asString(props.cta)} →</div>
  </SceneFrame>
);
