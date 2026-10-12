// Line icons stroked in currentColor (see the svg rule in styles.css).
export const BellIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z" /><path d="M10 20.5a2 2 0 0 0 4 0" /></svg>
);
export const LockIcon = () => (
  <svg viewBox="0 0 24 24"><rect x="5" y="11" width="14" height="10" rx="2.5" /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>
);
export const SoundIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z" /><path d="M16.5 8.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12" /></svg>
);
export const CloseIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M6 6l12 12M18 6L6 18" /></svg>
);
export const PowerIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M12 3v8" /><path d="M6.3 6.8a8 8 0 1 0 11.4 0" /></svg>
);
export const MicIcon = () => (  // slash shown while muted
  <svg viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3" /><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21" /><path class="slash" d="M4 4l16 16" /></svg>
);
export const SpeakerIcon = () => (  // slash shown while speaker is off
  <svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z" /><path d="M16.5 8.5a5 5 0 0 1 0 7" /><path class="slash" d="M4 4l16 16" /></svg>
);
export const CameraIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M4 8.5C4 7.7 4.7 7 5.5 7h2L9 5h6l1.5 2h2c.8 0 1.5.7 1.5 1.5v9c0 .8-.7 1.5-1.5 1.5h-13C4.7 19 4 18.3 4 17.5z" /><circle cx="12" cy="13" r="3.5" /></svg>
);
export const SendIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M12 19V5M6 11l6-6 6 6" /></svg>
);
export const HangupIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M3 13.6c5-4.4 13-4.4 18 0l-1.6 2.6c-.3.4-.8.6-1.3.4l-2.7-1.1c-.4-.2-.7-.6-.7-1l-.1-1.8a12 12 0 0 0-5.2 0l-.1 1.8c0 .4-.3.8-.7 1l-2.7 1.1c-.5.2-1 0-1.3-.4z" /></svg>
);
// Panel icons
export const TasksIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M5.6 18.4l2.1-2.1M16.3 7.7l2.1-2.1" /><circle cx="12" cy="12" r="3" /></svg>
);
export const TodosIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M10 6h10M10 12h10M10 18h10" /><path d="m3.5 6 1.5 1.5L7.5 5M3.5 12l1.5 1.5 2.5-2.5" /><circle cx="5" cy="18" r="1.2" /></svg>
);
export const ProjectsIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z" /></svg>
);
export const HistoryIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.4-5.7M4 4v4h4" /><path d="M12 8v4.5l3 2" /></svg>
);
export const SettingsIcon = () => (
  <svg viewBox="0 0 24 24">
    <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
    <circle cx="12" cy="12" r="3" />
  </svg>
);
export const HomeIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M4 10.5 12 4l8 6.5V19a1 1 0 0 1-1 1h-4.5v-5.5h-5V20H5a1 1 0 0 1-1-1v-8.5Z" /></svg>
);
export const BackIcon = () => <svg viewBox="0 0 24 24"><path d="M15 5 8 12l7 7" /></svg>;
export const ChevronIcon = () => <svg viewBox="0 0 24 24"><path d="m9 5 7 7-7 7" /></svg>;
export const CheckIcon = () => <svg viewBox="0 0 24 24"><path d="m5 12 5 5 9-10" /></svg>;
export const PlusIcon = () => <svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" /></svg>;
export const EditIcon = () => <svg viewBox="0 0 24 24"><path d="M4 20h4L19 9l-4-4L4 16v4Z" /></svg>;
export const TrashIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3" /></svg>
);
export const PlugIcon = () => <svg viewBox="0 0 24 24"><path d="M9 7V3M15 7V3M7 7h10v4a5 5 0 0 1-10 0V7ZM12 16v5" /></svg>;
export const PrefsIcon = () => <svg viewBox="0 0 24 24"><rect x="3" y="7" width="18" height="10" rx="5" /><circle cx="16" cy="12" r="2.5" /></svg>;
export const UserIcon = () => <svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4" /><path d="M4 21c1.5-4 4.5-6 8-6s6.5 2 8 6" /></svg>;
export const BrainIcon = () => (
  <svg viewBox="0 0 24 24"><path d="M9 4a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V5a3 3 0 0 0-3-1ZM15 4a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1" /></svg>
);
export const WordsIcon = () => <svg viewBox="0 0 24 24"><path d="M4 5h16v11H10l-5 4V5Z" /><path d="M8 9h8M8 12h5" /></svg>;
export const NoteIcon =() => <svg viewBox="0 0 24 24"><path d="M6 3h9l4 4v14H6V3Z" /><path d="M9 11h7M9 15h5" /></svg>;
export const SkillIcon = () => <svg viewBox="0 0 24 24"><path d="M13 3 5 13h6l-1 8 8-10h-6l1-8Z" /></svg>;
export const PulseIcon = () => <svg viewBox="0 0 24 24"><path d="M3 12h4l2-5 4 10 2-5h6" /></svg>;
export const KeyIcon = () => <svg viewBox="0 0 24 24"><circle cx="8" cy="15" r="4.5" /><path d="m11.2 11.8 8.8-8.8M16 7l3 3M13.5 9.5l2 2" /></svg>;
export const KeysIcon = () => (
  <svg viewBox="0 0 24 24"><rect x="2.5" y="6" width="19" height="12" rx="2.5" /><path d="M6.5 10h.01M10 10h.01M13.5 10h.01M17 10h.01M7.5 14h9" /></svg>
);
export const SearchIcon = () => <svg viewBox="0 0 24 24"><circle cx="11" cy="11" r="7" /><path d="m20 20-4-4" /></svg>;
export const RetryIcon = () => <svg viewBox="0 0 24 24"><path d="M4 12a8 8 0 1 0 2.5-5.8M4 4v5h5" /></svg>;
export const RepeatIcon = () => <svg viewBox="0 0 24 24"><path d="m17 2 4 4-4 4M3 11v-1a4 4 0 0 1 4-4h14M7 22l-4-4 4-4M21 13v1a4 4 0 0 1-4 4H3" /></svg>;
export const EyeIcon = () => <svg viewBox="0 0 24 24"><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z" /><circle cx="12" cy="12" r="3" /></svg>;
export const MachineIcon = () => <svg viewBox="0 0 24 24"><rect x="3" y="4" width="18" height="12" rx="2" /><path d="M9 20h6M12 16v4" /></svg>;
export const TerminalIcon = () => <svg viewBox="0 0 24 24"><rect x="3" y="4.5" width="18" height="15" rx="3" /><path d="m7.5 10 3 2.5-3 2.5M13 15h3.5" /></svg>;
export const BranchIcon = () => <svg viewBox="0 0 24 24"><circle cx="6" cy="5.5" r="2" /><circle cx="6" cy="18.5" r="2" /><circle cx="18" cy="8" r="2" /><path d="M6 7.5v9M18 10c0 4-6 3.5-11 7" /></svg>;
export const MoonIcon = () => <svg viewBox="0 0 24 24"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5Z" /></svg>;
export const ShieldIcon = () => <svg viewBox="0 0 24 24"><path d="M12 3 5 6v5c0 4.5 3 8.3 7 10 4-1.7 7-5.5 7-10V6z" /><path d="m9 12 2 2 4-4" /></svg>;
export const JarvisIcon = () => (  // the core: rings and a centre
  <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5" /><circle cx="12" cy="12" r="3" /><path d="M12 1.5v3M12 19.5v3M1.5 12h3M19.5 12h3" /></svg>
);
export const PauseIcon = () => <svg viewBox="0 0 24 24"><path d="M9 6v12M15 6v12" /></svg>;
export const PlayIcon = () => <svg viewBox="0 0 24 24"><path d="M8 5.5v13l10.5-6.5z" /></svg>;
export const StopIcon = () => <svg viewBox="0 0 24 24"><rect x="6" y="6" width="12" height="12" rx="2" /></svg>;
export const InfoIcon = () => <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="9" /><path d="M12 8h.01M11 12h1v5h1" /></svg>;
