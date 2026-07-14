#!/usr/bin/env Rscript
# =============================================================================
# Residual demand duration curve (RDC), reference style, across 41 weather years
# (1985-2025) from the single-node seasonal-storage run. 2x2 layout matching
# C:/Users/Eleanor/OneDrive/图片/duration curve.png :
#   top row    - deficit (residual demand) vs surplus regions, with P10-P90 band
#   bottom row - residual demand with storage cycling (discharge / charge bands)
# residual = demand - VRE - must-run firm; served by gas + imports + storage.
# =============================================================================

suppressPackageStartupMessages({
  library(data.table); library(ggplot2); library(arrow); library(patchwork)
})

WY  <- "C:/models/pypsa-gb/resources/analysis/weatheryears"
OUT <- "C:/models/pypsa-gb/resources/analysis"
NG  <- 400   # percentile grid points for the ensemble curves

# generation subtracted to form "residual demand" (VRE + must-run firm)
NONFLEX <- c("wind_onshore", "wind_offshore", "solar_pv", "marine", "nuclear",
             "biomass", "biogas", "landfill_gas", "sewage_gas", "waste_to_energy",
             "advanced_biofuel", "large_hydro", "geothermal")

proc_year <- function(ty) {
  files <- list.files(WY, pattern = sprintf("^%s_wy[0-9]+_genmix.parquet$", ty),
                      full.names = TRUE)
  pctg <- seq(0, 100, length.out = NG)
  pre <- post <- matrix(NA_real_, length(files), NG)
  S <- data.table()
  for (i in seq_along(files)) {
    d <- as.data.table(read_parquet(files[i]))
    nf <- rowSums(as.matrix(d[, intersect(NONFLEX, names(d)), with = FALSE]))
    res <- (d$demand - nf) / 1000                                     # GW residual demand
    net <- (d$storage_discharge + d$storage_charge +
            d$H2_turbine + d$electrolysis) / 1000                     # net storage output
    rp  <- res - net                                                  # after storage
    p <- 100 * (seq_along(res) - 0.5) / length(res)
    pre[i, ]  <- approx(p, sort(res, decreasing = TRUE), pctg, rule = 2)$y
    post[i, ] <- approx(p, sort(rp,  decreasing = TRUE), pctg, rule = 2)$y
    S <- rbind(S, data.table(
      rd = sum(pmax(res, 0)) / 1000, su = -sum(pmin(res, 0)) / 1000,
      rdp = sum(pmax(rp, 0)) / 1000, sup = -sum(pmin(rp, 0)) / 1000,
      pk = max(res), pkp = max(rp), mn = min(res), mnp = min(rp)))
  }
  q <- function(m, p) apply(m, 2, quantile, p, names = FALSE)
  list(cv = data.table(pct = pctg, pre_mean = colMeans(pre),
                        pre_lo = q(pre, .1), pre_hi = q(pre, .9),
                        post_mean = colMeans(post)),
       st = as.list(colMeans(S)))
}

D <- lapply(c("2030", "2040"), proc_year); names(D) <- c("2030", "2040")

NAVY <- "#22305f"; GREEN <- "#2e7d32"
base_t <- theme_classic(base_size = 10.5) +
  theme(plot.title = element_text(face = "bold", size = 11, hjust = 0.5),
        axis.line = element_line(linewidth = 0.4),
        legend.text = element_text(size = 7), legend.key.size = unit(9, "pt"),
        legend.background = element_rect(fill = alpha("white", 0.55), colour = NA),
        legend.key = element_rect(fill = NA))
xscale <- scale_x_continuous(limits = c(0, 100), expand = c(0, 0))

top_panel <- function(cv, st, ttl) {
  yr <- range(c(cv$pre_lo, cv$pre_hi))
  ggplot(cv, aes(pct)) +
    geom_ribbon(aes(ymin = pre_lo, ymax = pre_hi, fill = "P10–P90 across weather years")) +
    geom_ribbon(aes(ymin = 0, ymax = pmax(pre_mean, 0), fill = "Residual demand (must be served)")) +
    geom_ribbon(aes(ymin = pmin(pre_mean, 0), ymax = 0, fill = "Residual surplus (curtailable / storable)")) +
    geom_line(aes(y = pre_mean, colour = "Mean across weather years"), linewidth = 0.7) +
    geom_hline(yintercept = 0, colour = "grey55", linewidth = 0.3) +
    annotate("text", x = 5, y = 0.55 * yr[2], hjust = 0, size = 2.9, colour = "#7a1f1f",
             label = sprintf("Annual residual demand\n%.1f TWh/yr", st$rd)) +
    annotate("text", x = 60, y = 0.5 * yr[1], hjust = 0, size = 2.9, colour = "#1f5a2a",
             label = sprintf("Annual residual surplus\n%.1f TWh/yr", st$su)) +
    scale_fill_manual(NULL, values = c(
      "P10–P90 across weather years" = "grey82",
      "Residual demand (must be served)" = "#efa3a0",
      "Residual surplus (curtailable / storable)" = "#a9d5a0")) +
    scale_colour_manual(NULL, values = c("Mean across weather years" = NAVY)) +
    xscale + labs(title = ttl, x = "Percentage of hours (%)", y = "Residual demand (GW)") +
    base_t + theme(legend.position = c(0.99, 0.99), legend.justification = c(1, 1),
                   legend.spacing.y = unit(0, "pt"))
}

bot_panel <- function(cv, st, ttl) {
  cvp <- cv[pre_mean >= 0]; cvn <- cv[pre_mean < 0]
  ggplot(cv, aes(pct)) +
    geom_ribbon(aes(ymin = pre_lo, ymax = pre_hi, fill = "P10–P90 across weather years")) +
    geom_ribbon(data = cvp, aes(ymin = post_mean, ymax = pre_mean, fill = "Storage discharge (peak shaved)")) +
    geom_ribbon(data = cvn, aes(ymin = pre_mean, ymax = post_mean, fill = "Storage charge (surplus absorbed)")) +
    geom_line(aes(y = pre_mean,  colour = "Residual demand (no storage)"),   linewidth = 0.7) +
    geom_line(aes(y = post_mean, colour = "Residual demand (after storage)"), linewidth = 0.7, linetype = "22") +
    geom_hline(yintercept = 0, colour = "grey55", linewidth = 0.3) +
    annotate("text", x = 2, y = st$pk, hjust = 0, vjust = -0.5, size = 2.7, fontface = "bold",
             label = sprintf("Peak (no storage): %.0f GW", st$pk)) +
    annotate("text", x = 26, y = st$pk * 0.62, hjust = 0, size = 2.7, colour = GREEN,
             label = sprintf("Post-storage peak: %.0f GW\n(shaved %.0f GW)", st$pkp, st$pk - st$pkp)) +
    annotate("text", x = 99, y = st$mn * 0.9, hjust = 1, size = 2.7, colour = NAVY, fontface = "bold",
             label = sprintf("Max surplus: %.0f GW", st$mn)) +
    annotate("text", x = 72, y = st$mn * 0.28, hjust = 1, size = 2.7, colour = GREEN,
             label = if (abs(st$mnp) < 0.5) "After storage: surplus fully absorbed"
                     else sprintf("Post-storage surplus: %.0f GW", st$mnp)) +
    scale_fill_manual(NULL, values = c(
      "P10–P90 across weather years" = "grey85",
      "Storage discharge (peak shaved)" = "#5fbf5f",
      "Storage charge (surplus absorbed)" = "#c3e6ba")) +
    scale_colour_manual(NULL, values = c("Residual demand (no storage)" = NAVY,
                                         "Residual demand (after storage)" = GREEN)) +
    xscale + labs(title = ttl, x = "Percentage of hours (%)", y = "Residual demand (GW)") +
    base_t + theme(legend.position = c(0.02, 0.03), legend.justification = c(0, 0),
                   legend.spacing.y = unit(0, "pt"))
}

fig <- (top_panel(D[["2030"]]$cv, D[["2030"]]$st, "GB 2030 RDC: energy in deficit vs surplus regions") |
        top_panel(D[["2040"]]$cv, D[["2040"]]$st, "GB 2040 RDC: energy in deficit vs surplus regions")) /
       (bot_panel(D[["2030"]]$cv, D[["2030"]]$st, "GB 2030 residual demand duration curve with storage cycling") |
        bot_panel(D[["2040"]]$cv, D[["2040"]]$st, "GB 2040 residual demand duration curve with storage cycling"))

cap <- sprintf(paste0(
  "2030: annual residual demand %.1f → %.1f TWh/yr, surplus %.1f → %.1f TWh/yr.   ",
  "2040: residual demand %.1f → %.1f TWh/yr, surplus %.1f → %.1f TWh/yr.   ",
  "Mean and P10-P90 across 41 weather years (1985-2025); single-node run; residual = demand - VRE - must-run firm."),
  D[["2030"]]$st$rd, D[["2030"]]$st$rdp, D[["2030"]]$st$su, D[["2030"]]$st$sup,
  D[["2040"]]$st$rd, D[["2040"]]$st$rdp, D[["2040"]]$st$su, D[["2040"]]$st$sup)
fig <- fig + plot_annotation(caption = cap,
                             theme = theme(plot.caption = element_text(size = 7.5, hjust = 0)))

ggsave(file.path(OUT, "rdc_reference_style.pdf"), fig, width = 13, height = 8.5,
       units = "in", device = cairo_pdf)
ggsave(file.path(OUT, "rdc_reference_style.png"), fig, width = 13, height = 8.5,
       units = "in", dpi = 150)
message("wrote rdc_reference_style.pdf / .png")
print(rbindlist(lapply(names(D), function(y) c(year = y, lapply(D[[y]]$st, round, 1)))))
