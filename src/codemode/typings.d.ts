// Typed SDK exposed to model-written TypeScript inside the Bun sandbox.
// The Gemini brain writes code against these globals; we route the calls
// to the Python FastAPI gateway over fetch().

declare global {
  const crm: {
    /**
     * Look up vehicle owner + active policy by license plate (e.g. "B-AB-1234").
     * Returns null if not found.
     */
    lookupByPlate(plate: string): Promise<
      { owner: string; vehicle: string; policy_id: string } | null
    >;
  };

  const fraud: {
    /**
     * Score a candidate claim for fraud risk. score is 0..1; flags is a list
     * of human-readable warnings ("possibly-staged", "duplicate-near-recent", ...).
     */
    check(claim: {
      plate: string;
      description: string;
      location: string;
    }): Promise<{ score: number; flags: string[] }>;
  };

  const claimDb: {
    /**
     * Persist a finalized claim. Returns the assigned claim_id.
     */
    write(claim: object): Promise<{ claim_id: string }>;
  };

  const tavily: {
    /**
     * Real-time German P&C claim domain research via Tavily Research API.
     * Returns a structured summary scoped to the claim taxonomy + fraud
     * red-flags + missing-fact lookup.
     */
    research(query: string): Promise<{
      claim_taxonomy: string;
      fraud_red_flags: string[];
      missing_facts: string[];
    }>;
  };

  const photo: {
    /**
     * Describe a photo of vehicle damage; returns a short description and a
     * coarse severity bucket.
     */
    describe(url: string): Promise<{
      description: string;
      damage_severity: "low" | "medium" | "high";
    }>;
  };

  const dashboard: {
    /**
     * Push partial extracted facts to the live judges' dashboard. Non-blocking,
     * fire-and-forget. Call as soon as you've extracted anything new (plate,
     * location, injury status, other-party info, fraud signal, etc.). The
     * dashboard merges over prior state, so you can call it many times with
     * just the keys that changed.
     */
    update(facts: {
      caller_name?: string;
      reporter_role?: "policyholder" | "driver" | "claimant" | "third-party" | "broker" | "other";
      injuries?: { anyone_hurt: boolean; details?: string };
      policy_id?: string;
      plate?: string;
      vehicle?: string;
      vehicle_drivable?: boolean;
      location?: string;
      time_of_loss?: string;
      weather?: string;
      incident_type?: string;
      description?: string;
      other_party?: { plate?: string; name?: string; insurer?: string; admitted_fault?: boolean };
      police_on_scene?: boolean;
      witnesses?: string[];
      photos_available?: boolean;
      fraud_score?: number;
      fraud_flags?: string[];
      claim_id?: string;
      stage?: "greeting" | "triage" | "facts" | "lookup" | "wrap";
    }): Promise<{ ok: true }>;
  };
}

export {};
