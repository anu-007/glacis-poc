=========================================================================================
  BENCHMARK REPORT
=========================================================================================

  Test Case                                     gpt-4o-mini               gpt-5.4-nano            
  ────────────────────────────────────────────  ────────────────────────  ────────────────────────
  shipment_standard                             ✅ 100%  2.45s             ⚠️   33%  1.17s           
  shipment_delivered_extra_fields               ✅ 100%  1.83s             ✅ 100%  1.40s           
  shipment_exception                            ✅ 100%  2.00s             ✅ 100%  1.47s           
  shipment_snake_case_status_synonym            ✅ 100%  2.63s             ✅ 100%  1.20s           
  shipment_unmappable_status                    ✅ 100%  1.36s             ✅ 100%  1.09s           
  shipment_missing_tracking_number              ✅ 100%  5.29s             ✅ 100%  1.17s           
  invoice_standard                              ✅ 100%  1.55s             ✅ 100%  1.15s           
  invoice_alt_fields_string_amount_lowercase_currency  ✅ 100%  1.65s             ✅ 100%  1.48s           
  invoice_non_usd_currency                      ❌   0%  1.66s             ✅ 100%  1.26s           
  unclassified_order_event                      ✅ 100%  2.05s             ✅ 100%  1.53s           
  unclassified_heartbeat                        ✅ 100%  1.15s             ✅ 100%  1.06s           

  ─────────────────────────────────────────────────────────────────────────────────────────
  Metric                                gpt-4o-mini               gpt-5.4-nano            
  ─────────────────────────────────────────────────────────────────────────────────────────
  Accuracy (%)                          90.9000                   93.9000                 
  Avg Latency (s)                       2.1480                    1.2700                  
  P50 Latency (s)                       1.5490                    1.1980                  
  Avg Prompt Tokens                     1399                      1410                    
  Avg Cached Tokens                     1164                      0                       
  Avg Completion Tokens                 51                        46                      
  Cache Hit Rate (%)                    90.9000                   0.0000                  
  Avg Cost / Event ($)                  0.0002                    0.0003                  
  Cost / 1K Events ($)                  0.1591                    0.3398                  
  Total Benchmark Cost ($)              0.0053                    0.0112                  
  Total Cache Savings ($)               0.0027                    0.0000                  

═════════════════════════════════════════════════════════════════════════════════════════