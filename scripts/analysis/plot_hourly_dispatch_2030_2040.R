#!/usr/bin/env Rscript
# =============================================================================
# Hourly generation and storage for the PyPSA-GB whole-network runs, 2030 & 2040
#   FES 2025 Holistic Transition, Reduced 32-bus network, weather year 2010,
#   demand shape 2025. Wholesale (economic-dispatch) stage shown.
#
# Produces three figures, x-axis = calendar months (Jan to Dec), faceted by year:
#   fig1  hourly wind and solar generation
#   fig2  hourly generation mix by technology
#   fig3  hourly storage charging (negative) / discharging (positive) by technology
#
# Style: aggregated tables are left as objects in the global environment
# (vre_long, genmix_long, storage_long) for interactive inspection; only the
# final figures are written to disk. Nothing is hard-coded to a column count -
# technologies are detected from the data by pattern.
# =============================================================================

suppressPackageStartupMessages({
  library(data.table)
  library(ggplot2)
  library(ggsci)      # scientific-journal colour palettes (d3 category20 used below)
})

MARKET_DIR <- "C:/models/pypsa-gb/resources/market"
OUT_DIR    <- "C:/models/pypsa-gb/resources/analysis"
SCENARIOS  <- c("2030" = "HT30_disp_w2010", "2040" = "HT40_disp_w2010")

## ---- helpers ---------------------------------------------------------------

# Label each column by the first matching regex in `patterns`
# (names(patterns) = technology label, values = regex).
classify <- function(cols, patterns) {
  lab <- rep(NA_character_, length(cols))
  for (k in names(patterns)) {
    hit <- is.na(lab) & grepl(patterns[[k]], cols)
    lab[hit] <- k
  }
  lab
}

# Sum the columns of a wide data.table into per-technology hourly totals (GW),
# preserving the sign of the input (so storage charge stays negative).
per_tech <- function(dt, patterns) {
  cols <- setdiff(names(dt), "time")
  tech <- classify(cols, patterns)
  M <- as.matrix(dt[, ..cols])
  out <- data.table(time = dt$time)
  for (k in intersect(names(patterns), unique(na.omit(tech)))) {
    out[[k]] <- rowSums(M[, tech == k, drop = FALSE], na.rm = TRUE) / 1000
  }
  out
}

# Map real timestamps of any year onto a single reference year so 2030 and 2040
# share one Jan-Dec axis. 2020 is a leap year, so 29 Feb (2040) maps cleanly.
month_axis <- function(ts) {
  as.POSIXct(paste0("2020-", format(ts, "%m-%d %H:%M:%S")), tz = "UTC")
}

# Generator carrier -> display technology (order sets stacking / specificity)
GEN_PATTERNS <- c(
  wind             = "wind_onshore|wind_offshore|marine|tidal|wave",
  solar            = "solar_pv",
  nuclear          = "nuclear",
  `biomass & other`= "biomass|biogas|landfill_gas|sewage_gas|waste_to_energy|advanced_biofuel|large_hydro|small_hydro|geothermal|oil",
  interconnector   = "EU_supply|EU_import",   # columns are named "EU_supply_<country>_HVDC..."
  gas              = "CCGT|OCGT|gas_engine|CHP",
  unserved         = "load_shedding|unserved"
)

# Storage-unit carrier -> technology ("Battery" also catches "Domestic Battery")
STORAGE_PATTERNS <- c(
  BESS          = "Battery",
  `pumped hydro`= "Pumped[ _]Storage",   # columns use "Pumped_Storage" (underscores)
  LAES          = "LAES",
  CAES          = "CAES"
)

## ---- read + aggregate each scenario ----------------------------------------

read_scenario <- function(year, scn) {
  disp <- fread(file.path(MARKET_DIR, paste0(scn, "_wholesale_dispatch.csv")))
  sto  <- fread(file.path(MARKET_DIR, paste0(scn, "_wholesale_storage.csv")))
  lnk  <- fread(file.path(MARKET_DIR, paste0(scn, "_wholesale_links.csv")))
  setnames(disp, 1, "time"); setnames(sto, 1, "time"); setnames(lnk, 1, "time")
  disp[, time := as.POSIXct(time, tz = "UTC")]
  sto[,  time := as.POSIXct(time, tz = "UTC")]
  lnk[,  time := as.POSIXct(time, tz = "UTC")]

  gens <- per_tech(disp, GEN_PATTERNS)               # GW, by generation technology

  # hydrogen-to-power (from links, links_t.p0): electrolysis p0 = electricity in
  # (charge). H2_turbine p0 = H2 consumed (bus0=H2), so electricity delivered to
  # the grid = p0 x turbine efficiency 0.50 (else discharge is overstated 2x and
  # round-trip looks ~68% instead of the real 35% = 0.70 electrolysis x 0.50).
  lcols <- setdiff(names(lnk), "time"); Ml <- as.matrix(lnk[, ..lcols])
  h2_turbine <- rowSums(Ml[, grepl("^H2_turbine",   lcols), drop = FALSE]) / 1000 * 0.50
  electro    <- rowSums(Ml[, grepl("^electrolysis", lcols), drop = FALSE]) / 1000

  stor <- per_tech(sto, STORAGE_PATTERNS)            # GW, +discharge / -charge (signed)
  stor[, Hydrogen := h2_turbine - electro]          # add H2 store as a storage tech

  # demand reconstructed from the energy balance:
  #   demand = all generation + net storage output + (H2 turbine elec - electrolysis)
  gcols <- setdiff(names(disp), "time"); scols <- setdiff(names(sto), "time")
  allgen  <- rowSums(as.matrix(disp[, ..gcols]), na.rm = TRUE) / 1000
  sto_net <- rowSums(as.matrix(sto[,  ..scols]), na.rm = TRUE) / 1000
  demand  <- allgen + sto_net + h2_turbine - electro

  list(year = year, time = disp$time, gens = gens, stor = stor,
       h2_turbine = h2_turbine, demand = demand)
}

parts <- Map(read_scenario, names(SCENARIOS), SCENARIOS)

## ---- build long tables for plotting (left in the global environment) -------

vre_long <- rbindlist(lapply(parts, function(p) {
  d <- p$gens[, .(time, wind, solar)]
  d <- melt(d, id.vars = "time", variable.name = "technology", value.name = "GW")
  d[, year := p$year][]
}))

gen_techs <- c("wind", "solar", "nuclear", "biomass & other",
               "interconnector", "gas", "H2 turbine", "storage discharge")
genmix_long <- rbindlist(lapply(parts, function(p) {
  g <- copy(p$gens)
  g[, `H2 turbine` := pmax(p$h2_turbine, 0)]
  # storage discharge = sum of positive storage output across all storage techs
  disc <- p$stor[, setdiff(names(p$stor), "time"), with = FALSE]
  g[, `storage discharge` := rowSums(pmax(as.matrix(disc), 0))]
  keep <- intersect(gen_techs, names(g))
  d <- melt(g[, c("time", keep), with = FALSE], id.vars = "time",
            variable.name = "technology", value.name = "GW")
  d[, year := p$year][]
}))

storage_long <- rbindlist(lapply(parts, function(p) {
  d <- melt(p$stor, id.vars = "time", variable.name = "technology", value.name = "GW")
  d[, year := p$year][]
}))

# drop all-zero technologies (e.g. CAES if absent, unserved if none) and set order
for (nm in c("vre_long", "genmix_long", "storage_long")) {
  d <- get(nm)
  keep <- d[, .(s = sum(abs(GW))), by = technology][s > 1e-6, technology]
  assign(nm, d[technology %in% keep])
}
vre_long[,     plot_time := month_axis(time)]
genmix_long[,  plot_time := month_axis(time)]
storage_long[, plot_time := month_axis(time)]
genmix_long[,  technology := factor(technology, levels = gen_techs)]

## ---- aggregate to DAILY for readability ------------------------------------
# Generation: daily mean power (GW). Storage: keep discharge and charge separate
# (daily mean of the positive and negative parts) so intra-day cycling is not
# netted away. Daily tables are also left in the global environment.

to_daily <- function(d) d[, .(GW = mean(GW)),
                          by = .(day = as.Date(plot_time), technology, year)]
vre_daily     <- to_daily(vre_long)
genmix_daily  <- to_daily(genmix_long)
storage_daily <- storage_long[, .(discharge = mean(pmax(GW, 0)),
                                   charge    = mean(pmin(GW, 0))),
                              by = .(day = as.Date(plot_time), technology, year)]
genmix_daily[, technology := factor(technology, levels = gen_techs)]

# daily demand for the overlay line
demand_long <- rbindlist(lapply(parts, function(p)
  data.table(time = p$time, GW = p$demand, year = p$year)))
demand_long[, plot_time := month_axis(time)]
demand_daily <- demand_long[, .(GW = mean(GW)), by = .(day = as.Date(plot_time), year)]

# panel (b): generation mix EXCLUDING wind & solar (they dominate and have panel a)
gen_techs_b <- setdiff(gen_techs, c("wind", "solar"))
genmix_b <- genmix_daily[technology %in% gen_techs_b]
genmix_b[, technology := factor(technology, levels = gen_techs_b)]

## ---- colours + shared theme ------------------------------------------------

# ggsci d3 "category20": 10 bold colours then 10 light variants. Each technology
# gets a distinct hue so generation and storage never share a colour.
d3 <- pal_d3("category20")(20)
COL <- c(
  "wind"              = d3[1],   # blue
  "solar"             = d3[2],   # orange
  "nuclear"           = d3[5],   # purple
  "biomass & other"   = d3[3],   # green
  "interconnector"    = d3[6],   # brown
  "gas"               = d3[4],   # red
  "H2 turbine"        = d3[7],   # pink
  "storage discharge" = d3[8],   # grey
  "unserved"          = d3[14],  # salmon
  "BESS"              = d3[9],   # olive
  "pumped hydro"      = d3[10],  # cyan
  "LAES"              = d3[15],  # light purple
  "CAES"              = d3[13],  # light green
  "Hydrogen"          = d3[17])  # light pink

base_theme <- theme_minimal(base_size = 12) +
  theme(panel.grid.minor = element_blank(),
        strip.text = element_text(face = "bold"),
        axis.text.x = element_text(angle = 45, hjust = 1),
        legend.position = "bottom")
# narrower side-by-side panels: label every 2nd month to avoid crowding
month_x <- scale_x_date(date_breaks = "2 months", date_labels = "%b", expand = c(0, 0))

save_fig <- function(p, file, h = 5) {
  ggsave(file.path(OUT_DIR, file), p, width = 15, height = h, dpi = 120)
  message("wrote ", file.path(OUT_DIR, file)); invisible(file.path(OUT_DIR, file))
}

## ---- fig 1: wind and solar (daily) -----------------------------------------

fig1 <- ggplot(vre_daily, aes(day, GW, fill = technology)) +
  geom_area() +
  geom_line(data = demand_daily, aes(day, GW, colour = "demand"),
            inherit.aes = FALSE, linewidth = 0.4) +
  facet_grid(. ~ year) +   # 2030 left, 2040 right; shared (comparable) y-axis
  scale_fill_manual(values = COL) +
  scale_colour_manual(name = NULL, values = c("demand" = "black")) + month_x +
  labs(title = "Daily wind and solar generation - FES2025 HT, weather 2010",
       x = NULL, y = "GW (daily mean)", fill = NULL) + base_theme
f1 <- save_fig(fig1, "R_fig1_wind_solar_daily.png")

## ---- fig 2: generation mix by technology (daily) ---------------------------

fig2 <- ggplot(genmix_b, aes(day, GW, fill = technology)) +
  geom_area() +
  geom_line(data = demand_daily, aes(day, GW, colour = "demand"),
            inherit.aes = FALSE, linewidth = 0.4) +
  facet_grid(. ~ year) +   # 2030 left, 2040 right; shared (comparable) y-axis
  scale_fill_manual(values = COL, drop = FALSE) +
  scale_colour_manual(name = NULL, values = c("demand" = "black")) + month_x +
  labs(title = "Daily generation mix excl. wind & solar - FES2025 HT, weather 2010",
       x = NULL, y = "GW (daily mean)", fill = NULL) + base_theme
f2 <- save_fig(fig2, "R_fig2_generation_mix_daily.png")

## ---- fig 3: storage charge / discharge by technology (daily) ---------------

fig3 <- ggplot(storage_daily, aes(day, fill = technology)) +
  geom_area(aes(y = discharge)) +
  geom_area(aes(y = charge)) +
  geom_hline(yintercept = 0, linewidth = 0.3, colour = "grey30") +
  facet_grid(. ~ year) +   # 2030 left, 2040 right; shared (comparable) y-axis
  scale_fill_manual(values = COL) + month_x +
  labs(title = "Daily storage: discharging (+) and charging (-) by technology",
       subtitle = "FES2025 HT, weather 2010. BESS 2h, pumped hydro 8h, LAES 6h, Hydrogen (H2 store).",
       x = NULL, y = "GW (daily mean, +discharge / -charge)", fill = NULL) + base_theme
f3 <- save_fig(fig3, "R_fig3_storage_charge_discharge_daily.png")

## ---- combined A4 figure (all three panels on one page) ---------------------
# Stack (a) wind & solar, (b) generation mix, (c) storage into a single portrait
# A4 figure; 2030 left / 2040 right; shared x (months) shown once at the bottom.

suppressPackageStartupMessages(library(patchwork))
compact <- theme(legend.text     = element_text(size = 6.5),
                 legend.key.size = unit(7, "pt"),
                 legend.spacing.x = unit(2, "pt"),
                 legend.title    = element_blank(),
                 axis.title.y    = element_text(size = 9))
# panel (a) needs no legend of its own - wind/solar are covered by panel (b)'s
# generation legend, which is collected once and shown at the bottom.
pa <- fig1 + labs(title = NULL, y = "Wind & solar\n(GW)") + compact +
      theme(axis.text.x = element_blank(), axis.title.x = element_blank())
pb <- fig2 + labs(title = NULL, y = "Generation excl.\nwind & solar (GW)") + compact +
      guides(fill = guide_legend(nrow = 2)) +   # wrap fill legend to fit A4 width
      theme(axis.text.x = element_blank(), axis.title.x = element_blank(),
            strip.text = element_blank())
pc <- fig3 + labs(title = NULL, subtitle = NULL, y = "Storage\n(GW, +disch / -charge)") +
      compact + theme(strip.text = element_blank())

A4 <- (pa / pb / pc) +
  plot_layout(guides = "collect") +          # gather legends into one shared area
  plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")")   # no title
A4 <- A4 & theme(legend.position = "bottom", legend.box = "horizontal")

ggsave(file.path(OUT_DIR, "dispatch_A4_2030_2040.pdf"), A4,
       width = 8.27, height = 8, units = "in", device = cairo_pdf)
ggsave(file.path(OUT_DIR, "dispatch_A4_2030_2040.png"), A4,
       width = 8.27, height = 8, units = "in", dpi = 200)
message("wrote dispatch_A4_2030_2040.pdf / .png (A4 portrait)")

# no-text version: strip legend, axis titles/text and facet-strip labels, keeping
# the stacked-area panels, axes and the (a)-(c) tags for external labelling.
A4_nt <- (pa / pb / pc) +
  plot_annotation(tag_levels = "a", tag_prefix = "(", tag_suffix = ")")
A4_nt <- A4_nt & theme(legend.position = "none",
                       axis.title.x = element_blank(), axis.title.y = element_blank(),
                       axis.text.x = element_blank(), axis.text.y = element_blank(),
                       axis.ticks = element_blank(),
                       strip.text = element_blank(), plot.title = element_blank(),
                       plot.tag = element_text(face = "bold", size = 13))
ggsave(file.path(OUT_DIR, "dispatch_A4_2030_2040_notext.pdf"), A4_nt,
       width = 8.27, height = 8, units = "in", device = cairo_pdf)
ggsave(file.path(OUT_DIR, "dispatch_A4_2030_2040_notext.png"), A4_nt,
       width = 8.27, height = 8, units = "in", dpi = 200)
message("wrote dispatch_A4_2030_2040_notext.pdf / .png")

## ---- 1. surplus absorbed by each storage type ------------------------------
# Storage charging (the negative part of each storage tech's signed output) is
# the surplus it absorbs. Sorted highest-to-lowest and stacked, so the coloured
# area between the curve and the x-axis is the energy each type takes up.
absorb <- rbindlist(lapply(parts, function(p) {
  sc <- as.matrix(p$stor[, setdiff(names(p$stor), "time"), with = FALSE])
  charge <- pmax(-sc, 0)                                   # GW absorbed, by type
  ord <- order(rowSums(charge), decreasing = TRUE)
  dt <- as.data.table(charge[ord, , drop = FALSE])
  dt[, `:=`(pct = 100 * (seq_len(.N) - 0.5) / .N, year = p$year)][]
}))
absorb_long <- melt(absorb, id.vars = c("pct", "year"),
                    variable.name = "technology", value.name = "GW")
absorbed_twh <- absorb_long[, .(TWh = round(sum(GW) / 1000, 1)),
                            by = .(technology, year)]        # energy absorbed
print(absorbed_twh[order(year, -TWh)])

fig_absorb <- ggplot(absorb_long, aes(pct, GW, fill = technology)) +
  geom_area() +
  facet_grid(. ~ year) +
  scale_fill_manual(values = COL) +
  labs(x = "% of hours (sorted by total charging, highest to lowest)",
       y = "Storage charging (GW) - surplus absorbed",
       title = "Surplus absorbed by each storage type - FES2025 HT, weather 2010",
       fill = NULL) +
  base_theme + theme(legend.position = "bottom")
ggsave(file.path(OUT_DIR, "R_surplus_absorption_by_storage.png"), fig_absorb,
       width = 12, height = 5, dpi = 130)
ggsave(file.path(OUT_DIR, "R_surplus_absorption_by_storage.pdf"), fig_absorb,
       width = 12, height = 5, device = cairo_pdf)
message("wrote R_surplus_absorption_by_storage")

## ---- 2. full generation breakdown, with and without the demand line --------
fig_full <- ggplot(genmix_daily, aes(day, GW, fill = technology)) +
  geom_area() +
  facet_grid(. ~ year) +
  scale_fill_manual(values = COL, drop = FALSE) + month_x +
  labs(x = NULL, y = "GW (daily mean)",
       title = "Daily generation mix, all technologies - FES2025 HT, weather 2010",
       fill = NULL) + base_theme
ggsave(file.path(OUT_DIR, "R_full_generation_no_demand.png"), fig_full,
       width = 12, height = 5, dpi = 130)
ggsave(file.path(OUT_DIR, "R_full_generation_no_demand.pdf"), fig_full,
       width = 12, height = 5, device = cairo_pdf)

fig_full_d <- fig_full +
  geom_line(data = demand_daily, aes(day, GW, colour = "demand"),
            inherit.aes = FALSE, linewidth = 0.4) +
  scale_colour_manual(name = NULL, values = c("demand" = "black"))
ggsave(file.path(OUT_DIR, "R_full_generation_with_demand.png"), fig_full_d,
       width = 12, height = 5, dpi = 130)
ggsave(file.path(OUT_DIR, "R_full_generation_with_demand.pdf"), fig_full_d,
       width = 12, height = 5, device = cairo_pdf)
message("wrote R_full_generation_no_demand / _with_demand")

## ---- self-contained HTML report of all figures -----------------------------

img_uri <- function(path) {
  if (requireNamespace("base64enc", quietly = TRUE))
    paste0("data:image/png;base64,", base64enc::base64encode(path))
  else basename(path)   # fallback: reference by name (keep PNGs beside the HTML)
}
panel <- function(title, path, note = "")
  sprintf("<section><h2>%s</h2>%s<img src='%s'></section>",
          title, if (nzchar(note)) paste0("<p class='note'>", note, "</p>") else "",
          img_uri(path))

figs <- c(f1, f2, f3)
titles <- c("1. Wind and solar generation (daily)",
            "2. Generation mix by technology (daily)",
            "3. Storage charging / discharging by technology (daily)")
notes <- c("Daily-mean output; summer solar and winter wind, with the August wind lull.",
           "All modelled generating technologies. Interconnectors are modelled here (EU imports).",
           "Positive = discharge, negative = charge. BESS, pumped hydro, LAES and hydrogen (H2 store).")
dunkel <- file.path(OUT_DIR, "pypsagb_dunkelflaute_2040.png")
extra <- if (file.exists(dunkel))
  panel("4. Extreme dunkelflaute detail - 2040 (hourly)", dunkel,
        "Three-week window around the worst low-wind, low-solar spell; hydrogen-to-power and storage cover demand.") else ""

html <- paste0(
  "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
  "<meta name='viewport' content='width=device-width, initial-scale=1'>",
  "<title>PyPSA-GB whole-network dispatch - FES2025 HT 2030 & 2040</title><style>",
  "body{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1200px;",
  "margin:24px auto;padding:0 18px;color:#1a1d21;background:#fcfcfb}",
  "h1{font-size:24px} h2{font-size:18px;margin-top:8px} .note{color:#5a6068;font-size:14px;margin:2px 0 8px}",
  "img{width:100%;height:auto;border:1px solid #e7e6e2;border-radius:8px}",
  "section{margin:0 0 28px} .meta{color:#8b929b;font-size:13px}</style></head><body>",
  "<h1>PyPSA-GB whole-network dispatch - FES 2025 Holistic Transition, 2030 & 2040</h1>",
  "<p class='meta'>Reduced 32-bus network, weather year 2010, demand shape 2025, ",
  "two-stage rolling market (wholesale stage shown). Each figure faceted 2030 (top) / 2040 (bottom).</p>",
  paste(mapply(panel, titles, figs, notes), collapse = "\n"), extra,
  "</body></html>")
html_path <- file.path(OUT_DIR, "dispatch_report_2030_2040.html")
writeLines(html, html_path); message("wrote ", html_path)

message("done. Objects in global env: vre_daily, genmix_daily, storage_daily")
