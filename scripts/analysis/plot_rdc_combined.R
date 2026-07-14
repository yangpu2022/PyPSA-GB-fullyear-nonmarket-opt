#!/usr/bin/env Rscript
# =============================================================================
# Combined residual-demand duration-curve figure (2x2), 2030 | 2040.
#
#   TOP ROW  - gap-style residual across weather years (P10-P90 band):
#              residual = demand - VRE - must-run firm, sorted into a duration
#              curve; red = residual demand met by DISPATCHABLE, green = residual
#              SURPLUS from VRE. Ensemble = 41 weather years (single-node run;
#              the stand-alone scripts/gap tool covers 2010-2024 only, so the
#              41-year range comes from the single-node pre-storage residual,
#              which uses the identical demand - VRE - firm definition).
#
#   BOTTOM   - whole-network full-year optimisation (FES 2025 HT, weather 2010):
#              residual demand duration curve broken down BY STORAGE TECHNOLOGY
#              (Battery / Pumped hydro / LAES / Hydrogen), showing how much each
#              charges (band above the no-storage curve) and discharges (band
#              below), with total residual demand and peak demand annotated.
#
# Fixes applied: (2) red/green top-row areas are semi-transparent so the grey
# P10-P90 band shows through; (4) 2030 and 2040 share one y-axis within each row.
# =============================================================================

suppressPackageStartupMessages({
  library(data.table); library(ggplot2); library(arrow); library(patchwork); library(ggsci)
})

WY     <- "C:/models/pypsa-gb/resources/analysis/weatheryears"
MARKET <- "C:/models/pypsa-gb/resources/market"
OUT    <- "C:/models/pypsa-gb/resources/analysis"
NG     <- 400

NAVY <- "#22305f"; DRED <- "#7a1f1f"; DGRN <- "#1f5a2a"

# generation subtracted to form the residual (VRE + must-run firm)
VRE  <- c("wind_onshore", "wind_offshore", "solar_pv", "marine")
FIRM <- c("nuclear", "biomass", "biogas", "landfill_gas", "sewage_gas",
          "waste_to_energy", "advanced_biofuel", "large_hydro", "geothermal")

# ============================ TOP ROW (ensemble gap residual) ================
proc_top <- function(ty) {
  files <- list.files(WY, pattern = sprintf("^%s_wy[0-9]+_genmix.parquet$", ty),
                      full.names = TRUE)
  pctg <- seq(0, 100, length.out = NG)
  pre <- matrix(NA_real_, length(files), NG); S <- data.table()
  for (i in seq_along(files)) {
    d  <- as.data.table(read_parquet(files[i]))
    v  <- rowSums(as.matrix(d[, intersect(VRE,  names(d)), with = FALSE]))
    fm <- rowSums(as.matrix(d[, intersect(FIRM, names(d)), with = FALSE]))
    res <- (d$demand - v - fm) / 1000                        # GW residual demand
    p   <- 100 * (seq_along(res) - 0.5) / length(res)
    pre[i, ] <- approx(p, sort(res, decreasing = TRUE), pctg, rule = 2)$y
    S <- rbind(S, data.table(rd = sum(pmax(res, 0)) / 1000,
                             su = -sum(pmin(res, 0)) / 1000, pk = max(res)))
  }
  q <- function(m, p) apply(m, 2, quantile, p, names = FALSE)
  list(cv = data.table(pct = pctg, mean = colMeans(pre),
                       lo = q(pre, .1), hi = q(pre, .9)),
       st = list(rd = mean(S$rd), su = mean(S$su), pk = mean(S$pk),
                 nyr = length(files)))
}

# ============================ BOTTOM ROW (whole-network by tech) =============
GEN_P <- c(wind = "wind_onshore|wind_offshore|marine|tidal|wave", solar = "solar_pv",
           nuclear = "nuclear",
           firm = "biomass|biogas|landfill_gas|sewage_gas|waste_to_energy|advanced_biofuel|large_hydro|small_hydro|geothermal|oil")
STO_P <- c(Battery = "Battery", `Pumped hydro` = "Pumped[ _]Storage",
           LAES = "LAES", CAES = "CAES")
STO_TECHS <- c("Battery", "Pumped hydro", "LAES", "Hydrogen")   # stack order

classify <- function(cols, patterns) {
  lab <- rep(NA_character_, length(cols))
  for (k in names(patterns)) { hit <- is.na(lab) & grepl(patterns[[k]], cols); lab[hit] <- k }
  lab
}
per_tech <- function(dt, patterns) {
  cols <- setdiff(names(dt), "time"); tech <- classify(cols, patterns)
  M <- as.matrix(dt[, ..cols]); out <- data.table(time = dt$time)
  for (k in intersect(names(patterns), unique(na.omit(tech))))
    out[[k]] <- rowSums(M[, tech == k, drop = FALSE], na.rm = TRUE) / 1000
  out
}

proc_bot <- function(scn) {
  disp <- fread(file.path(MARKET, paste0(scn, "_wholesale_dispatch.csv")))
  sto  <- fread(file.path(MARKET, paste0(scn, "_wholesale_storage.csv")))
  lnk  <- fread(file.path(MARKET, paste0(scn, "_wholesale_links.csv")))
  setnames(disp, 1, "time"); setnames(sto, 1, "time"); setnames(lnk, 1, "time")

  g <- per_tech(disp, GEN_P)
  lcols <- setdiff(names(lnk), "time"); Ml <- as.matrix(lnk[, ..lcols])
  h2t <- rowSums(Ml[, grepl("^H2_turbine",   lcols), drop = FALSE]) / 1000
  ele <- rowSums(Ml[, grepl("^electrolysis", lcols), drop = FALSE]) / 1000
  st  <- per_tech(sto, STO_P); st[, Hydrogen := h2t - ele]        # +discharge / -charge

  gcols <- setdiff(names(disp), "time"); scols <- setdiff(names(sto), "time")
  demand <- rowSums(as.matrix(disp[, ..gcols]), na.rm = TRUE) / 1000 +
            rowSums(as.matrix(sto[, ..scols]),  na.rm = TRUE) / 1000 + h2t - ele
  vre  <- g$wind + g$solar
  firm <- g$nuclear + g$firm
  res  <- demand - vre - firm                                    # residual, no storage

  techs <- intersect(STO_TECHS, names(st))
  N <- as.matrix(st[, ..techs])                                  # hour x tech signed net
  ord <- order(res, decreasing = TRUE)
  res <- res[ord]; N <- N[ord, , drop = FALSE]

  # Bin the residual-sorted hours into percentile bins and average within each
  # bin: storage dispatch is not monotonic in residual, so per-hour bands would
  # be noise. Binned means give a readable envelope of typical storage behaviour
  # at each demand level.
  NBIN <- 120
  bin  <- pmin(NBIN, floor(NBIN * (seq_along(res) - 1) / length(res)) + 1)
  resb <- as.numeric(tapply(res, bin, mean))
  Nb   <- apply(N, 2, function(col) as.numeric(tapply(col, bin, mean)))
  Nb   <- matrix(Nb, ncol = length(techs), dimnames = list(NULL, techs))
  pct  <- as.numeric(tapply(100 * (seq_along(res) - 0.5) / length(res), bin, mean))

  # stack each tech's net off the no-storage curve: discharge (net>0) lowers,
  # charge (net<0) raises; final level = residual after all storage.
  bands <- rbindlist(lapply(seq_along(techs), function(j) {
    top <- resb - (if (j == 1) 0 else rowSums(Nb[, seq_len(j - 1), drop = FALSE]))
    bot <- top - Nb[, j]
    data.table(pct = pct, tech = techs[j],
               ymin = pmin(top, bot), ymax = pmax(top, bot))
  }))
  bands[, tech := factor(tech, levels = techs)]
  after <- resb - rowSums(Nb)

  # stats from full-resolution (unbinned) series so TWh totals and peaks are exact
  after_full <- res - rowSums(N)
  tw <- function(x) sum(x) / 1e3   # GWh summed over hours (values are GW) -> TWh
  perc <- data.table(tech = techs,
    dis = sapply(techs, function(t) tw(pmax(st[[t]], 0))),
    cha = sapply(techs, function(t) tw(-pmin(st[[t]], 0))))
  list(line = data.table(pct = pct, res = resb, after = after),
       bands = bands, perc = perc,
       st = list(rd = tw(pmax(res, 0)), su = tw(-pmin(res, 0)),
                 pk = max(res), pkp = max(after_full)))
}

D30t <- proc_top("2030"); D40t <- proc_top("2040")
D30b <- proc_bot("HT30_disp_w2010"); D40b <- proc_bot("HT40_disp_w2010")

# shared y-limits per row (fix #4)
yl_top <- range(c(D30t$cv$lo, D30t$cv$hi, D40t$cv$lo, D40t$cv$hi))
yl_bot <- range(c(D30b$line$res, D40b$line$res, D30b$bands$ymin, D40b$bands$ymin,
                  D30b$bands$ymax, D40b$bands$ymax))

# storage-tech colours (clearly distinct - fix from earlier "too similar")
STO_COL <- c("Battery" = "#1f77b4", "Pumped hydro" = "#17becf",
             "LAES" = "#9467bd", "Hydrogen" = "#e377c2")

base_t <- theme_classic(base_size = 10.5) +
  theme(plot.title = element_text(face = "bold", size = 11, hjust = 0.5),
        axis.line = element_line(linewidth = 0.4),
        legend.text = element_text(size = 7), legend.key.size = unit(9, "pt"),
        legend.background = element_rect(fill = alpha("white", 0.6), colour = NA),
        legend.key = element_rect(fill = NA))
xsc <- scale_x_continuous(limits = c(0, 100), expand = c(0, 0))

# direct-labelling replaces legends throughout (scientific-figure best practice):
# every series is named next to the curve/area it marks, so the reader never
# looks away to a legend box.
no_leg <- theme(legend.position = "none")

top_panel <- function(D, ttl) {
  cv <- D$cv; st <- D$st
   span <- yl_top[2] - yl_top[1]
  ggplot(cv, aes(pct)) +
    geom_ribbon(aes(ymin = 0, ymax = pmax(mean, 0)), fill = "#d62728", alpha = 0.42) +
    geom_ribbon(aes(ymin = pmin(mean, 0), ymax = 0), fill = "#2ca02c", alpha = 0.42) +
    geom_ribbon(aes(ymin = lo, ymax = hi), fill = alpha("grey45", 0.5)) +
    geom_line(aes(y = mean), colour = NAVY, linewidth = 0.7) +
    geom_hline(yintercept = 0, colour = "grey55", linewidth = 0.3) +
    # red area (deficit) label, inside the red region
    annotate("text", x = 6, y = 0.52 * yl_top[2], hjust = 0, size = 2.9, colour = DRED, fontface = "bold",
             label = sprintf("Residual demand\nmet by dispatchable\n%.1f TWh/yr", st$rd)) +
    # green area (surplus) label, inside the green region
    annotate("text", x = 60, y = 0.52 * yl_top[1], hjust = 0, size = 2.9, colour = DGRN, fontface = "bold",
             label = sprintf("Residual surplus\nfrom VRE\n%.1f TWh/yr", st$su)) +
    # direct labels for the two ensemble series (in white space above the curve)
    annotate("text", x = 33, y = 0.46 * yl_top[2], hjust = 0, size = 2.6, colour = NAVY,
             label = "Mean of 41 weather years") +
    annotate("text", x = 33, y = 0.46 * yl_top[2] - 0.11 * span, hjust = 0, size = 2.6, colour = "grey30",
             label = "P10-P90 band") +
    scale_y_continuous(limits = yl_top) + xsc +
    labs(title = ttl, x = "Percentage of hours (%)", y = "Residual demand (GW)") +
    base_t + no_leg
}

bot_panel <- function(D, ttl) {
  ln <- D$line; bd <- D$bands; st <- D$st; pc <- D$perc
  span <- yl_bot[2] - yl_bot[1]
  # compact colour-keyed table (top-right): swatch + tech + charge/discharge TWh.
  # This replaces the verbose legend and carries the numbers at the same time.
  pc <- pc[match(STO_TECHS, pc$tech, nomatch = 0)]
  ty  <- 0.98 * yl_bot[2] - (seq_len(nrow(pc))) * 0.082 * span
  tbl <- data.table(tech = pc$tech, y = ty, col = STO_COL[pc$tech],
                    lab = sprintf("%s   %.0f / %.0f", pc$tech, pc$cha, pc$dis))
  ggplot() +
    geom_ribbon(data = bd, aes(pct, ymin = ymin, ymax = ymax, fill = tech)) +
    geom_line(data = ln, aes(pct, res),   colour = NAVY, linewidth = 0.7) +
    geom_line(data = ln, aes(pct, after), colour = "#111111", linewidth = 0.6, linetype = "22") +
    geom_hline(yintercept = 0, colour = "grey55", linewidth = 0.3) +
    scale_fill_manual(values = STO_COL) +
    # peak, in words, right at the peak
    annotate("text", x = 3, y = st$pk, hjust = 0, vjust = -0.3, size = 2.7, fontface = "bold",
             label = sprintf("Peak demand: %.0f GW\n(%.0f GW after storage)", st$pk, st$pkp)) +
    # total residual demand, in the white space below the deficit curves
    annotate("text", x = 24, y = 0.09 * yl_bot[2], hjust = 0, size = 2.7, colour = DRED,
             label = sprintf("Total residual demand\n%.1f TWh/yr", st$rd)) +
    # residual surplus, in the empty space below the shallow mid bands
    annotate("text", x = 40, y = 0.86 * yl_bot[1], hjust = 0, size = 2.7, colour = DGRN,
             label = sprintf("Residual surplus %.1f TWh/yr\n(absorbed by storage charging)", st$su)) +
    # direct labels for the two curves, in clear space near each
    annotate("text", x = 62, y = 0.12 * yl_bot[2], hjust = 0, size = 2.5, colour = "#111111",
             label = "after storage") +
    annotate("text", x = 84, y = 0.78 * yl_bot[1], hjust = 0, size = 2.5, colour = NAVY,
             label = "no storage") +
    # storage-tech table
    annotate("text", x = 58, y = 0.98 * yl_bot[2], hjust = 0, size = 2.5, fontface = "bold",
             label = "Storage  charge / discharge (TWh)") +
    geom_point(data = tbl, aes(x = 59, y = y), colour = tbl$col, size = 2.2) +
    geom_text(data = tbl, aes(x = 62, y = y, label = lab), colour = tbl$col, hjust = 0, size = 2.5) +
    scale_y_continuous(limits = yl_bot) + xsc +
    labs(title = ttl, x = "Percentage of hours (%)", y = "Residual demand (GW)") +
    base_t + no_leg
}

fig <- (top_panel(D30t, "GB 2030  -  residual demand across weather years") |
        top_panel(D40t, "GB 2040  -  residual demand across weather years")) /
       (bot_panel(D30b, "GB 2030  -  residual demand with storage by technology") |
        bot_panel(D40b, "GB 2040  -  residual demand with storage by technology"))

cap <- sprintf(paste0(
  "Top: mean and P10-P90 residual demand (demand - VRE - must-run firm) across %d weather years, single-node run. ",
  "Bottom: whole-network full-year optimisation, FES 2025 Holistic Transition, weather 2010; ",
  "bands show each storage technology's hourly charge (above) / discharge (below) along the no-storage duration curve."),
  D30t$st$nyr)
fig <- fig + plot_annotation(caption = cap,
             theme = theme(plot.caption = element_text(size = 7.5, hjust = 0)))

ggsave(file.path(OUT, "rdc_combined.pdf"), fig, width = 13, height = 9, units = "in", device = cairo_pdf)
ggsave(file.path(OUT, "rdc_combined.png"), fig, width = 13, height = 9, units = "in", dpi = 150)
message("wrote rdc_combined.pdf / .png")
cat(sprintf("TOP  2030: rd=%.1f su=%.1f pk=%.1f (n=%d yr)\n", D30t$st$rd, D30t$st$su, D30t$st$pk, D30t$st$nyr))
cat(sprintf("TOP  2040: rd=%.1f su=%.1f pk=%.1f (n=%d yr)\n", D40t$st$rd, D40t$st$su, D40t$st$pk, D40t$st$nyr))
cat(sprintf("BOT  2030: rd=%.1f su=%.1f pk=%.1f pkAfter=%.1f\n", D30b$st$rd, D30b$st$su, D30b$st$pk, D30b$st$pkp))
cat(sprintf("BOT  2040: rd=%.1f su=%.1f pk=%.1f pkAfter=%.1f\n", D40b$st$rd, D40b$st$su, D40b$st$pk, D40b$st$pkp))
cat("BOT 2030 per-tech (TWh dis/cha):\n"); print(D30b$perc)
cat("BOT 2040 per-tech (TWh dis/cha):\n"); print(D40b$perc)
