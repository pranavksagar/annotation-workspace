import { Upload } from 'lucide-react';
import { useRef, useState } from 'react';
import { api, ApiError } from '../lib/annotationApi';

export type UploadOutcome = {drawingId: string; duplicate: boolean; filename: string};

export function UploadDrawing({onUploaded}: {onUploaded: (outcome: UploadOutcome) => void}) {
  const input = useRef<HTMLInputElement>(null);
  const [progress, setProgress] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const choose = async (file: File | undefined) => {
    if (!file) return;
    setError(null);
    setProgress(0);
    try {
      const result = await api.upload(file, setProgress);
      onUploaded({drawingId: result.drawing.id, duplicate: result.duplicate, filename: result.drawing.filename});
    } catch (e) {
      setError(e instanceof ApiError ? e.message : 'Upload failed.');
    } finally {
      setProgress(null);
      if (input.current) input.current.value = '';
    }
  };
  return <div className="upload">
    <button className="primary" onClick={() => input.current?.click()} disabled={progress !== null}>
      <Upload size={16}/>{progress !== null ? `Uploading… ${Math.round(progress * 100)}%` : 'Upload drawing'}
    </button>
    <input ref={input} type="file" hidden accept=".pdf,.png,.jpg,.jpeg,application/pdf,image/png,image/jpeg"
      onChange={e => void choose(e.target.files?.[0])} aria-label="Drawing file"/>
    {progress !== null && <div className="progress" aria-label="Upload progress"><span style={{width: `${progress * 100}%`}}/></div>}
    <small className="muted">PDF, PNG or JPEG, one file at a time. Native CAD files (DWG, DXF, STEP) must be exported to PDF first.</small>
    {error && <p className="error" role="alert">{error}</p>}
  </div>;
}
