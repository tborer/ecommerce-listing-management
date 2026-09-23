export type Rule = { rule: string; ok: boolean; detail: string };

export type Candidate = {
  id: number;
  status: string;
  source: string;
  ebay_item_id: string;
  ebay_title: string;
  ebay_price: number;
  ebay_url: string;
  ebay_image_url: string | null;
  ebay_discount_pct: number | null;
  cj_title: string | null;
  cj_variant_name: string | null;
  cj_url: string | null;
  cj_image_url: string | null;
  cj_cost: number | null;
  shipping_cost: number | null;
  shipping_method: string | null;
  shipping_days_max: number | null;
  match_score: number | null;
  match_reasons: string[];
  list_price: number | null;
  profit: number | null;
  margin_pct: number | null;
  criteria: Rule[];
  passes: boolean;
  auto_listed: boolean;
  ebay_listing_id: string | null;
  ebay_listing_url: string | null;
  error: string | null;
  updated_at: string | null;
};

export type Run = {
  id: number;
  trigger: string;
  status: "running" | "done" | "error";
  phase: string;
  stats: Record<string, number | string[]>;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
};

export type Dashboard = {
  counts: Record<string, number>;
  active_run: Run | null;
  last_run: Run | null;
  schedule: { enabled: boolean; hour_utc: number; next_slot_at: string | null };
  auto_list: { enabled: boolean; max_per_run: number };
  connections: { cj: boolean; ebay: boolean };
};

export type Settings = {
  deal_categories: string[];
  keywords: string[];
  items_per_run: number;
  min_ebay_price: number;
  max_ebay_price: number;
  fee_pct: number;
  target_margin_pct: number;
  min_profit: number;
  max_shipping_cost: number;
  max_delivery_days: number;
  min_match_score: number;
  cj_warehouse: "any" | "US";
  price_undercut_pct: number;
  schedule_enabled: boolean;
  schedule_hour_utc: number;
  auto_list_enabled: boolean;
  auto_list_max_per_run: number;
  ebay_fulfillment_policy_id: string | null;
  ebay_payment_policy_id: string | null;
  ebay_return_policy_id: string | null;
  location_city: string | null;
  location_state: string | null;
  location_postal_code: string | null;
  location_country: string;
};
