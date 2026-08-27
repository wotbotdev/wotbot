import { python as pythonLanguage } from '@codemirror/lang-python';

import { CodeEditor } from '@/components/code-editor';
import { Textarea } from '@/components/ui/textarea';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';

import { type JobActionKind } from './job-form-model';

const pythonExtensions = [pythonLanguage()];

interface JobActionFieldsProps {
  actionKind: JobActionKind;
  analysisCode: string;
  prompt: string;
  onAnalysisCodeChange: (value: string) => void;
  onPromptChange: (value: string) => void;
  onActionKindChange?: (value: JobActionKind) => void;
  showLabels?: boolean;
}

function PromptField({
  prompt,
  onPromptChange,
  showLabel,
}: Pick<JobActionFieldsProps, 'onPromptChange' | 'prompt'> & {
  showLabel: boolean;
}) {
  return (
    <div className="space-y-2">
      {showLabel ? <label className="text-sm font-medium">Prompt</label> : null}
      <Textarea
        rows={9}
        value={prompt}
        onChange={(event) => onPromptChange(event.target.value)}
        placeholder="Summarize the latest shipment delays and exception alerts."
      />
    </div>
  );
}

function AnalysisCodeField({
  analysisCode,
  onAnalysisCodeChange,
  showLabel,
}: Pick<JobActionFieldsProps, 'analysisCode' | 'onAnalysisCodeChange'> & {
  showLabel: boolean;
}) {
  return (
    <div className="space-y-2">
      {showLabel ? (
        <label className="text-sm font-medium">Analysis code</label>
      ) : null}
      <CodeEditor
        className="text-[13px]"
        extensions={pythonExtensions}
        height="22rem"
        loadingLabel="Loading code"
        value={analysisCode}
        onChange={onAnalysisCodeChange}
      />
    </div>
  );
}

export function JobActionFields({
  actionKind,
  analysisCode,
  prompt,
  onActionKindChange,
  onAnalysisCodeChange,
  onPromptChange,
  showLabels = true,
}: JobActionFieldsProps) {
  if (!onActionKindChange) {
    return actionKind === 'analysis' ? (
      <AnalysisCodeField
        analysisCode={analysisCode}
        onAnalysisCodeChange={onAnalysisCodeChange}
        showLabel={showLabels}
      />
    ) : (
      <PromptField
        prompt={prompt}
        onPromptChange={onPromptChange}
        showLabel={showLabels}
      />
    );
  }

  return (
    <Tabs
      value={actionKind}
      onValueChange={(value) => onActionKindChange(value as JobActionKind)}
      className="space-y-4"
    >
      <TabsList className="grid w-full grid-cols-2 sm:w-fit">
        <TabsTrigger value="prompt">Prompt</TabsTrigger>
        <TabsTrigger value="analysis">Analysis</TabsTrigger>
      </TabsList>

      <TabsContent value="prompt" className="mt-0">
        <PromptField
          prompt={prompt}
          onPromptChange={onPromptChange}
          showLabel={showLabels}
        />
      </TabsContent>

      <TabsContent value="analysis" className="mt-0">
        <AnalysisCodeField
          analysisCode={analysisCode}
          onAnalysisCodeChange={onAnalysisCodeChange}
          showLabel={showLabels}
        />
      </TabsContent>
    </Tabs>
  );
}
