import {
  ButtonItem,
  PanelSection,
  PanelSectionRow,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { callable, definePlugin, toaster } from "@decky/api";
import { useCallback, useEffect, useRef, useState } from "react";
import { FaDesktop } from "react-icons/fa";

type Profile = {
  id: string;
  name: string;
  system: string;
  verified: boolean;
  enabled: boolean;
};

type State = {
  enabled: boolean;
  profiles: Profile[];
  sunshine_port?: number;
  error?: string;
};

const getState = callable<[], State>("get_state");
const setEnabled = callable<[enabled: boolean, profile?: string], State>("set_enabled");
const restore = callable<[], State>("restore");

function notifyError(state: State | undefined) {
  if (state?.error) {
    toaster.toast({ title: "SDSS", body: state.error });
  }
}

function Content() {
  const [state, setState] = useState<State>();
  const [busy, setBusy] = useState(false);
  // The panel is unmounted as soon as the user swipes away, which routinely happens while
  // an `sdss enable` is still running. Writing state after that is a React warning and,
  // worse, hides the fact that the result was thrown away.
  const mounted = useRef(true);
  // A poll must never fire mid-write, or it would clobber the toggle the user just moved
  // with a status read taken before the change landed.
  const busyRef = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const apply = useCallback(async (action: () => Promise<State>) => {
    busyRef.current = true;
    setBusy(true);
    try {
      const next = await action();
      if (!mounted.current) return;
      setState(next);
      notifyError(next);
    } catch (error) {
      toaster.toast({ title: "SDSS", body: String(error) });
    } finally {
      busyRef.current = false;
      if (mounted.current) setBusy(false);
    }
  }, []);

  const refresh = useCallback(() => void apply(getState), [apply]);

  useEffect(() => {
    refresh();
    // The CLI can also be driven from a terminal or by the session itself, so poll while
    // the panel is open rather than trusting the state captured when it was first shown.
    const timer = setInterval(() => {
      if (!busyRef.current) refresh();
    }, 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const profiles = state?.profiles ?? [];

  return (
    <PanelSection title="Second Screen">
      <PanelSectionRow>
        <ToggleField
          label="Enable SDSS"
          description="Route supported emulators through the streamed second screen."
          checked={state?.enabled ?? false}
          disabled={busy}
          onChange={(enabled: boolean) => void apply(() => setEnabled(enabled))}
        />
      </PanelSectionRow>

      {profiles.map((profile) => (
        <PanelSectionRow key={profile.id}>
          <ToggleField
            label={profile.name}
            description={profile.verified ? profile.system : `${profile.system} (unverified)`}
            checked={profile.enabled}
            disabled={busy || !state?.enabled}
            onChange={(enabled: boolean) =>
              void apply(() => setEnabled(enabled, profile.id))
            }
          />
        </PanelSectionRow>
      ))}

      <PanelSectionRow>
        <ButtonItem
          layout="below"
          disabled={busy}
          onClick={() => void apply(restore)}
        >
          Disable SDSS and restore configs
        </ButtonItem>
      </PanelSectionRow>
    </PanelSection>
  );
}

export default definePlugin(() => ({
  name: "SDSS",
  titleView: <div className={staticClasses.Title}>Steam Deck Second Screen</div>,
  content: <Content />,
  icon: <FaDesktop />,
  onDismount() {},
}));
