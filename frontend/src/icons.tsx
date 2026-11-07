// A few stroke icons, drawn inline so the app needs no icon font or network request.
const PATHS = {
  mail: "M3 6h18v12H3z M3 7l9 6 9-6",
  question: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M9.5 9.5a2.5 2.5 0 0 1 4.9.7c0 1.7-2.4 2.1-2.4 3.6 M12 17h.01",
  check: "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18z M8 12.5l2.7 2.7L16 10",
  erp: "M4 6c0-1.7 3.6-3 8-3s8 1.3 8 3-3.6 3-8 3-8-1.3-8-3z M4 6v12c0 1.7 3.6 3 8 3s8-1.3 8-3V6 M4 12c0 1.7 3.6 3 8 3s8-1.3 8-3",
  archive: "M3 4h18v4H3z M5 8v12h14V8 M10 12h4",
  back: "M15 6l-6 6 6 6",
  sliders: "M4 7h10 M18 7h2 M4 17h4 M12 17h8 M14 5v4 M8 15v4",
  clip: "M15.5 7.5l-6.4 6.4a1.5 1.5 0 0 0 2.1 2.1l6.4-6.4a3 3 0 0 0-4.2-4.2l-6.4 6.4a4.5 4.5 0 0 0 6.4 6.4l6.1-6.1",
  inbox: "M3 13l3-8h12l3 8v6H3z M3 13h5l1 2h6l1-2h5",
  doc: "M6 3h8l4 4v14H6z M14 3v4h4 M9 12h6 M9 16h6",
  arrow: "M5 12h14 M13 6l6 6-6 6",
} as const;

export type IconName = keyof typeof PATHS;

export function Icon({ name, className = "" }: { name: IconName; className?: string }) {
  return (
    <svg className={`i ${className}`} viewBox="0 0 24 24" aria-hidden="true">
      <path d={PATHS[name]} />
    </svg>
  );
}
