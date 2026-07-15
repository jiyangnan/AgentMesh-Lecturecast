import React from 'react';
import {
  BeforeAfter,
  CodeWalkthrough,
  ConceptCards,
  EndingSummary,
  HookOutcome,
  HookQuestion,
  MetricChange,
  ProcessFlow,
  ProductUIFocus,
  SectionTitle,
  StepFocus,
} from './components';
import {SceneComponentProps} from './types';

export const componentRegistry: Record<string, React.FC<SceneComponentProps>> = {
  'hook.outcome.v1': HookOutcome,
  'hook.question.v1': HookQuestion,
  'section.title.v1': SectionTitle,
  'concept.cards.v1': ConceptCards,
  'comparison.before_after.v1': BeforeAfter,
  'diagram.process_flow.v1': ProcessFlow,
  'tutorial.step_focus.v1': StepFocus,
  'product.ui_focus.v1': ProductUIFocus,
  'data.metric_change.v1': MetricChange,
  'code.walkthrough.v1': CodeWalkthrough,
  'ending.summary_cta.v1': EndingSummary,
};

