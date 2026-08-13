/**
 * SVGO v3+ Configuration
 * 
 * This configuration provides comprehensive SVG optimization while preserving
 * important attributes needed for frontend rendering.
 * 
 * @see https://svgo.dev/docs/configuration/
 */

module.exports = {
  // Disable multipass to match single-pass SVGO optimization in py-png2svg-ms
  multipass: false,
  
  // Set precision for number rounding (lower = more compression, less precision)
  js2svg: {
    indent: 2,
    pretty: false
  },
  
  plugins: [
    {
      // Use the default preset with customizations
      name: 'preset-default',
      params: {
        overrides: {
          // Disable removeViewBox to ensure proper scaling
          // Our frontend needs viewBox for responsive rendering
          removeViewBox: false,
          
          // Keep IDs for potential frontend references
          cleanupIds: {
            remove: false,
            minify: true
          },
          
          // Preserve high floating-point precision for sharp CAD/floor-plan vectors
          convertPathData: {
            floatPrecision: 5,
            transformPrecision: 5
          },
          
          // Clean up numeric values without truncating vector precision
          cleanupNumericValues: {
            floatPrecision: 5
          },
          
          // Convert colors to shorter formats
          convertColors: {
            currentColor: true,
            names2hex: true,
            rgb2hex: true,
            shorthex: true,
            shortname: true
          }
        }
      }
    },
    
    // Additional custom plugins
    {
      name: 'removeScriptElement'
    },
    {
      name: 'removeStyleElement'
    },
    {
      name: 'sortAttrs',
      params: {
        xmlnsOrder: 'alphabetical'
      }
    }
  ]
};

