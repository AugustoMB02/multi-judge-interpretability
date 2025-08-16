#!/usr/bin/env python3
"""
Multi-Judge vs Persona Correlation Experiment

This script runs experiments to test the core research question:
Do Martian API judges correlate with human persona preferences?

Features:
1. Uses Martian API judges
2. Loads UltraFeedback dataset or existing persona data
3. Tests with/without normalization
4. Run-based organization with complete tracking

Usage:
  python run_full_experiment.py --data-source ultrafeedback --data-size 100 --dry-run
  python run_full_experiment.py --data-source personas --data-size 50
"""

import asyncio
import json
import pickle
import random
from datetime import datetime
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from pathlib import Path
from typing import Dict, List, Any, Optional, Tuple
from sklearn.model_selection import train_test_split
from sklearn.preprocessing import StandardScaler
from sklearn.metrics import mean_squared_error, mean_absolute_error, r2_score
import argparse
from dotenv import load_dotenv

# Load environment variables for API access
load_dotenv()

# Import project modules
from pipeline.core.dataset_loader import DatasetLoader
from pipeline.core.persona_simulation import PersonaSimulator, PERSONAS
from pipeline.core.judge_evaluation import JudgeEvaluator, JUDGE_IDS
from pipeline.core.aggregator_training import MLPTrainer, GAMAggregator, compute_metrics, load_training_config, determine_training_scale
from utils.logging_setup import (
    setup_universal_logging, log_experiment_start, log_experiment_progress,
    log_experiment_milestone, log_experiment_complete, log_model_results,
    log_data_validation
)


class FullExperiment:
    """
    Complete multi-judge vs persona correlation experiment with run tracking.
    """
    
    def __init__(
        self,
        data_source: str = "ultrafeedback",
        data_size: int = 100,
        test_size: float = 0.2,
        random_seed: int = 42,
        concurrency: int = 1,  # Reduced for API rate limiting
        checkpoint_interval: int = 10,
        normalize_features: bool = True,
        run_name: Optional[str] = None
    ):
        self.data_source = data_source
        self.data_size = data_size
        self.test_size = test_size
        self.random_seed = random_seed
        self.concurrency = concurrency
        self.checkpoint_interval = checkpoint_interval
        self.normalize_features = normalize_features
        
        # Set random seeds
        random.seed(random_seed)
        np.random.seed(random_seed)
        
        # Create run-specific directories
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.run_name = run_name or f"{data_source}_{data_size}samples_{timestamp}"
        self.run_dir = Path("full_experiment_runs") / self.run_name
        
        # Create subdirectories
        self.run_dir.mkdir(parents=True, exist_ok=True)
        (self.run_dir / "data").mkdir(exist_ok=True)
        (self.run_dir / "results").mkdir(exist_ok=True)
        (self.run_dir / "logs").mkdir(exist_ok=True)
        (self.run_dir / "plots").mkdir(exist_ok=True)
        (self.run_dir / "checkpoints").mkdir(exist_ok=True)
        
        # Initialize components
        self.dataset_loader = DatasetLoader()
        self.judge_evaluator = None
        self.scaler = StandardScaler() if normalize_features else None
        
        # Save configuration
        self.config = {
            'data_source': data_source,
            'data_size': data_size,
            'test_size': test_size,
            'random_seed': random_seed,
            'concurrency': concurrency,
            'checkpoint_interval': checkpoint_interval,
            'normalize_features': normalize_features,
            'experiment_type': 'JUDGES_VS_PERSONAS',
            'run_name': self.run_name,
            'timestamp': timestamp
        }
        
        # Save config to run directory
        with open(self.run_dir / "config.json", 'w') as f:
            json.dump(self.config, f, indent=2)
        
        # Set up logging
        self.log_info = setup_universal_logging(
            experiment_name=f"full_experiment_{self.run_name}",
            log_dir=str(self.run_dir / "logs")
        )
        
        log_experiment_start(self.config)
        
        print(f"🚀 Starting experiment run: {self.run_name}")
        print(f"📁 Run directory: {self.run_dir}")
    
    def load_and_prepare_data(self) -> pd.DataFrame:
        """Load data based on source and prepare experiment subset."""
        log_experiment_milestone(f"Loading Data from Source: {self.data_source}")
        
        if self.data_source == "ultrafeedback":
            # Load fresh UltraFeedback data
            data = self.dataset_loader.load_ultrafeedback(
                n_samples=self.data_size * 2,  # Load extra to ensure enough after filtering
                random_seed=self.random_seed
            )
            
            # Note: This data won't have persona scores yet
            log_experiment_milestone("UltraFeedback data loaded - persona simulation will be needed")
            
        elif self.data_source == "personas":
            # Load existing data with persona annotations
            personas_path = "data/data_with_all_personas.pkl"
            data = self.dataset_loader.load_existing_personas(personas_path)
            
            log_experiment_milestone("Existing persona data loaded")
            
        else:
            raise ValueError(f"Unknown data source: {self.data_source}")
        
        # Create experiment subset
        subset = self.dataset_loader.create_experiment_subset(
            data,
            n_samples=self.data_size,
            random_seed=self.random_seed,
            output_path=str(self.run_dir / "data" / "experiment_subset.pkl")
        )
        
        # Validate data structure
        valid_samples = len(subset)
        has_personas = 'human_feedback' in subset.columns
        
        log_data_validation("Experiment Data", len(subset), valid_samples, {
            'data_source': self.data_source,
            'has_persona_annotations': has_personas,
            'expected_columns': ['instruction', 'answer'],
            'actual_columns': list(subset.columns),
            'saved_to': str(self.run_dir / "data" / "experiment_subset.pkl")
        })
        
        return subset
    
    async def simulate_personas_if_needed(self, data: pd.DataFrame) -> pd.DataFrame:
        """Simulate persona responses if not already present."""
        if 'human_feedback' in data.columns:
            log_experiment_milestone("Persona annotations already present, skipping simulation")
            return data
        
        log_experiment_milestone("Running Persona Simulation for UltraFeedback Data")
        
        # Initialize persona simulator
        persona_simulator = PersonaSimulator()
        
        # Run simulation with checkpointing
        data_with_personas = await persona_simulator.simulate_dataset(
            data,
            question_col='instruction',
            answer_col='answer',
            concurrency=self.concurrency,
            checkpoint_interval=self.checkpoint_interval,
            checkpoint_dir=self.run_dir / "checkpoints"
        )
        
        # Save personas data
        personas_path = self.run_dir / "data" / "data_with_personas.pkl"
        with open(personas_path, 'wb') as f:
            pickle.dump(data_with_personas, f)
        
        log_experiment_milestone(f"Persona simulation complete, saved to {personas_path}")
        return data_with_personas
    
    def initialize_judges(self):
        """Initialize connection to Martian API judges."""
        log_experiment_milestone("Initializing Martian API Judges")
        
        try:
            self.judge_evaluator = JudgeEvaluator()
            
            # Validate judge loading
            num_judges_loaded = len(self.judge_evaluator.judges)
            expected_judges = len(JUDGE_IDS)
            
            log_data_validation("Martian Judge Loading", expected_judges, num_judges_loaded, {
                'judge_ids': list(self.judge_evaluator.judges.keys()),
                'missing_judges': [j for j in JUDGE_IDS if j not in self.judge_evaluator.judges],
                'api_source': 'Martian API'
            })
            
            if num_judges_loaded == 0:
                raise ValueError("No judges loaded from Martian API. Check API credentials and judge deployment.")
            
            return True
            
        except Exception as e:
            log_experiment_milestone(f"Failed to Initialize Judges: {e}")
            raise
    
    def run_judge_inference(self, data: pd.DataFrame) -> pd.DataFrame:
        """Run judge inference via Martian API."""
        log_experiment_milestone("Running Judge Inference")
        
        # Check if already done
        judge_file = self.run_dir / "data" / "data_with_judge_scores.pkl"
        if judge_file.exists():
            log_experiment_milestone("Found Existing Judge Scores")
            with open(judge_file, 'rb') as f:
                return pickle.load(f)
        
        # Initialize judges if not done
        if self.judge_evaluator is None:
            self.initialize_judges()
        
        # Run evaluation with checkpointing
        data_with_judges = self.judge_evaluator.evaluate_dataset(
            data,
            question_col='instruction',
            answer_col='answer',
            checkpoint_dir=self.run_dir / "checkpoints",
            checkpoint_interval=self.checkpoint_interval,
            max_workers=self.concurrency
        )
        
        # Save results
        with open(judge_file, 'wb') as f:
            pickle.dump(data_with_judges, f)
        
        # Validate judge scores
        valid_samples = 0
        for idx, row in data_with_judges.iterrows():
            if 'judge_scores' in row and isinstance(row['judge_scores'], list) and len(row['judge_scores']) == len(JUDGE_IDS):
                valid_samples += 1
        
        log_data_validation("Judge Inference", len(data_with_judges), valid_samples, {
            'judges_used': len(JUDGE_IDS),
            'api_calls_made': len(data_with_judges) * len(JUDGE_IDS),
            'source': 'Martian API inference',
            'saved_to': str(judge_file)
        })
        
        return data_with_judges
    
    def analyze_correlations(self, data: pd.DataFrame) -> Dict[str, Any]:
        """Analyze correlations between judges and persona scores."""
        log_experiment_milestone("Analyzing Judge-Persona Correlations")
        
        judge_averages = []
        persona_averages = []
        individual_judge_scores = {judge_id: [] for judge_id in JUDGE_IDS}
        individual_persona_scores = {persona: [] for persona in PERSONAS.keys()}
        
        for idx, row in data.iterrows():
            if ('human_feedback' in row and 'personas' in row['human_feedback'] and
                'judge_scores' in row and isinstance(row['judge_scores'], list)):
                
                # Get persona average
                personas_feedback = row['human_feedback']['personas']
                persona_scores = [p['score'] for p in personas_feedback.values() if 'score' in p and p['score'] is not None]
                if not persona_scores:
                    continue
                persona_avg = np.mean(persona_scores)
                
                # Get judge average
                judge_scores = row['judge_scores']
                if len(judge_scores) != len(JUDGE_IDS):
                    continue
                judge_avg = np.mean(judge_scores)
                
                judge_averages.append(judge_avg)
                persona_averages.append(persona_avg)
                
                # Store individual scores
                for i, judge_id in enumerate(JUDGE_IDS):
                    individual_judge_scores[judge_id].append(judge_scores[i])
                
                for persona_name, feedback in personas_feedback.items():
                    if 'score' in feedback and feedback['score'] is not None:
                        individual_persona_scores[persona_name].append(feedback['score'])
        
        if len(judge_averages) < 2:
            log_experiment_milestone("Insufficient Data for Correlation Analysis")
            return {}
        
        # Overall correlation
        overall_correlation = np.corrcoef(judge_averages, persona_averages)[0, 1]
        
        # Individual judge correlations
        judge_correlations = {}
        for judge_id in JUDGE_IDS:
            if len(individual_judge_scores[judge_id]) >= 2:
                corr = np.corrcoef(individual_judge_scores[judge_id], persona_averages)[0, 1]
                judge_correlations[judge_id] = corr
        
        # Individual persona correlations  
        persona_correlations = {}
        for persona_name in PERSONAS.keys():
            if len(individual_persona_scores[persona_name]) >= 2:
                corr = np.corrcoef(individual_persona_scores[persona_name], judge_averages)[0, 1]
                persona_correlations[persona_name] = corr
        
        correlation_analysis = {
            'overall_correlation': overall_correlation,
            'judge_correlations': judge_correlations,
            'persona_correlations': persona_correlations,
            'judge_range': (np.min(judge_averages), np.max(judge_averages)),
            'persona_range': (np.min(persona_averages), np.max(persona_averages)),
            'num_samples': len(judge_averages),
            'judge_scores_raw': judge_averages,
            'persona_scores_raw': persona_averages
        }
        
        # Save correlation analysis
        with open(self.run_dir / "results" / "correlation_analysis.json", 'w') as f:
            # Convert numpy types for JSON serialization
            serializable_analysis = {}
            for key, value in correlation_analysis.items():
                if isinstance(value, (np.ndarray, list)):
                    serializable_analysis[key] = [float(x) if isinstance(x, np.number) else x for x in value]
                elif isinstance(value, dict):
                    serializable_analysis[key] = {k: float(v) if isinstance(v, np.number) else v for k, v in value.items()}
                elif isinstance(value, tuple):
                    serializable_analysis[key] = [float(x) for x in value]
                else:
                    serializable_analysis[key] = float(value) if isinstance(value, np.number) else value
            json.dump(serializable_analysis, f, indent=2)
        
        log_data_validation("Correlation Analysis", len(judge_averages), len(judge_averages), {
            'overall_correlation': f"{overall_correlation:.4f}",
            'correlation_strength': 'strong' if abs(overall_correlation) > 0.7 else 'moderate' if abs(overall_correlation) > 0.3 else 'weak',
            'judge_range': f"{np.min(judge_averages):.2f} - {np.max(judge_averages):.2f}",
            'persona_range': f"{np.min(persona_averages):.2f} - {np.max(persona_averages):.2f}",
            'best_judge': max(judge_correlations.items(), key=lambda x: abs(x[1])) if judge_correlations else None,
            'best_persona': max(persona_correlations.items(), key=lambda x: abs(x[1])) if persona_correlations else None
        })
        
        return correlation_analysis
    
    def test_aggregation_models(self, data: pd.DataFrame) -> Dict[str, Any]:
        """Test aggregation models with judge scores."""
        log_experiment_milestone("Testing Aggregation Models")
        
        # Prepare training data with uniform persona sampling
        X_list = []
        y_list = []
        
        # Uniform persona sampling
        available_personas = list(PERSONAS.keys())
        samples_per_persona = len(data) // len(available_personas)
        remaining_samples = len(data) % len(available_personas)
        
        persona_assignment = []
        for persona in available_personas:
            persona_assignment.extend([persona] * samples_per_persona)
        for _ in range(remaining_samples):
            persona_assignment.append(random.choice(available_personas))
        random.shuffle(persona_assignment)
        
        # Extract features and targets
        for idx, (row, assigned_persona) in enumerate(zip(data.iterrows(), persona_assignment)):
            row = row[1]
            
            if ('human_feedback' not in row or 'personas' not in row['human_feedback'] or
                'judge_scores' not in row or not isinstance(row['judge_scores'], list)):
                continue
            
            personas_feedback = row['human_feedback']['personas']
            if assigned_persona not in personas_feedback or 'score' not in personas_feedback[assigned_persona]:
                continue
            
            selected_score = personas_feedback[assigned_persona]['score']
            judge_scores = row['judge_scores']
            
            if selected_score is None or len(judge_scores) != len(JUDGE_IDS):
                continue
            
            X_list.append(judge_scores)
            y_list.append(selected_score)
        
        if len(X_list) < 10:
            log_experiment_milestone(f"Insufficient Data for Model Training: {len(X_list)} samples")
            return {}
        
        X = np.array(X_list)
        y = np.array(y_list)
        
        # Test with and without normalization
        results = {}
        
        for normalize in [False, True]:
            norm_suffix = "_normalized" if normalize else "_raw"
            
            X_test = X.copy()
            if normalize:
                scaler = StandardScaler()
                X_test = scaler.fit_transform(X_test)
            
            # Split data
            X_train, X_val, y_train, y_val = train_test_split(
                X_test, y, test_size=self.test_size, random_state=self.random_seed
            )
            
            if len(X_train) < 5:
                continue
            
            # Train MLP with config-based parameters
            try:
                # Load training config and determine scale
                training_config = load_training_config()
                scale = determine_training_scale(len(X_train))
                mlp_config = training_config["mlp_training"].get(scale, training_config["mlp_training"]["medium_scale"])
                
                log_experiment_milestone(f"Using {scale} MLP config: {mlp_config}")
                
                mlp_trainer = MLPTrainer(
                    hidden_dim=mlp_config["hidden_dim"],
                    learning_rate=mlp_config["learning_rate"],
                    batch_size=min(mlp_config["batch_size"], max(2, len(X_train) // 2)),
                    n_epochs=mlp_config["n_epochs"]
                )
                
                train_losses, val_losses = mlp_trainer.fit(X_train, y_train, X_val, y_val)
                
                train_pred = mlp_trainer.predict(X_train)
                val_pred = mlp_trainer.predict(X_val)
                
                results[f'mlp{norm_suffix}'] = {
                    'train_metrics': compute_metrics(y_train, train_pred),
                    'test_metrics': compute_metrics(y_val, val_pred),
                    'normalization': normalize
                }
                
                log_model_results(f"MLP{norm_suffix}", 
                                results[f'mlp{norm_suffix}']['train_metrics'], 
                                results[f'mlp{norm_suffix}']['test_metrics'])
                
            except Exception as e:
                log_experiment_milestone(f"MLP Training Failed{norm_suffix}: {e}")
        
        # Save model results
        with open(self.run_dir / "results" / "model_results.json", 'w') as f:
            # Convert numpy types for JSON serialization
            serializable_results = {}
            for key, value in results.items():
                serializable_results[key] = {}
                for metric_type, metrics in value.items():
                    if isinstance(metrics, dict):
                        serializable_results[key][metric_type] = {k: float(v) if isinstance(v, np.number) else v for k, v in metrics.items()}
                    else:
                        serializable_results[key][metric_type] = metrics
            json.dump(serializable_results, f, indent=2)
        
        return results
    
    def create_visualizations(self, correlation_analysis: Dict[str, Any], model_results: Dict[str, Any]):
        """Create comprehensive visualizations."""
        log_experiment_milestone("Creating Visualizations")
        
        if not correlation_analysis:
            return
        
        fig = plt.figure(figsize=(15, 10))
        
        # 1. Overall correlation scatter plot
        plt.subplot(2, 3, 1)
        judge_scores = correlation_analysis['judge_scores_raw']
        persona_scores = correlation_analysis['persona_scores_raw']
        plt.scatter(judge_scores, persona_scores, alpha=0.7)
        plt.xlabel('Average Judge Score')
        plt.ylabel('Average Persona Score')
        plt.title(f'Judges vs Personas\nr = {correlation_analysis["overall_correlation"]:.3f}')
        
        # Add correlation line
        if len(judge_scores) > 1:
            z = np.polyfit(judge_scores, persona_scores, 1)
            p = np.poly1d(z)
            plt.plot(judge_scores, p(judge_scores), "r--", alpha=0.8)
        
        # 2. Individual judge correlations
        plt.subplot(2, 3, 2)
        judge_corrs = correlation_analysis.get('judge_correlations', {})
        if judge_corrs:
            judges = list(judge_corrs.keys())
            correlations = list(judge_corrs.values())
            bars = plt.bar(range(len(judges)), correlations)
            plt.xticks(range(len(judges)), [j.replace('-judge', '') for j in judges], rotation=45, ha='right')
            plt.ylabel('Correlation with Persona Avg')
            plt.title('Individual Judge Correlations')
            plt.axhline(y=0, color='black', linestyle='-', alpha=0.3)
            
            # Color bars
            for bar, corr in zip(bars, correlations):
                if abs(corr) > 0.5:
                    bar.set_color('green')
                elif abs(corr) > 0.3:
                    bar.set_color('orange')
                else:
                    bar.set_color('red')
        
        # 3. Individual persona correlations
        plt.subplot(2, 3, 3)
        persona_corrs = correlation_analysis.get('persona_correlations', {})
        if persona_corrs:
            personas = list(persona_corrs.keys())
            correlations = list(persona_corrs.values())
            bars = plt.bar(range(len(personas)), correlations)
            plt.xticks(range(len(personas)), personas, rotation=45, ha='right')
            plt.ylabel('Correlation with Judge Avg')
            plt.title('Individual Persona Correlations')
            plt.axhline(y=0, color='black', linestyle='-', alpha=0.3)
        
        # 4. Model performance comparison
        plt.subplot(2, 3, 4)
        if model_results:
            model_names = []
            r2_scores = []
            
            for model_name, results in model_results.items():
                if 'test_metrics' in results:
                    model_names.append(model_name.replace('_', '\n'))
                    r2_scores.append(results['test_metrics']['r2'])
            
            if model_names:
                bars = plt.bar(model_names, r2_scores)
                plt.ylabel('Test R² Score')
                plt.title('Model Performance\n(Raw vs Normalized)')
                plt.axhline(y=0, color='red', linestyle='--', alpha=0.5)
                
                for bar, score in zip(bars, r2_scores):
                    if score > 0.3:
                        bar.set_color('green')
                    elif score > 0:
                        bar.set_color('orange')
                    else:
                        bar.set_color('red')
        
        # 5. Score distributions
        plt.subplot(2, 3, 5)
        plt.hist(judge_scores, bins=10, alpha=0.7, label='Judge Scores', color='blue')
        plt.hist(persona_scores, bins=10, alpha=0.7, label='Persona Scores', color='red')
        plt.xlabel('Score')
        plt.ylabel('Frequency')
        plt.title('Score Distributions')
        plt.legend()
        
        # 6. Summary statistics
        plt.subplot(2, 3, 6)
        plt.text(0.1, 0.9, f"Overall Correlation: {correlation_analysis['overall_correlation']:.3f}", 
                transform=plt.gca().transAxes, fontsize=12, weight='bold')
        plt.text(0.1, 0.8, f"Judge Range: {correlation_analysis['judge_range'][0]:.2f} - {correlation_analysis['judge_range'][1]:.2f}", 
                transform=plt.gca().transAxes)
        plt.text(0.1, 0.7, f"Persona Range: {correlation_analysis['persona_range'][0]:.2f} - {correlation_analysis['persona_range'][1]:.2f}", 
                transform=plt.gca().transAxes)
        plt.text(0.1, 0.6, f"Samples: {correlation_analysis['num_samples']}", 
                transform=plt.gca().transAxes)
        
        if judge_corrs:
            best_judge = max(judge_corrs.items(), key=lambda x: abs(x[1]))
            plt.text(0.1, 0.5, f"Best Judge: {best_judge[0]}", 
                    transform=plt.gca().transAxes)
            plt.text(0.1, 0.4, f"Best Judge Corr: {best_judge[1]:.3f}", 
                    transform=plt.gca().transAxes)
        
        if model_results:
            best_model = max([(k, v['test_metrics']['r2']) for k, v in model_results.items() if 'test_metrics' in v], 
                           key=lambda x: x[1], default=None)
            if best_model:
                plt.text(0.1, 0.3, f"Best Model: {best_model[0]}", 
                        transform=plt.gca().transAxes)
                plt.text(0.1, 0.2, f"Best R²: {best_model[1]:.3f}", 
                        transform=plt.gca().transAxes)
        
        plt.axis('off')
        plt.title('Experiment Summary')
        
        plt.tight_layout()
        
        # Save plot
        plot_path = self.run_dir / "plots" / 'experiment_analysis.png'
        plt.savefig(plot_path, dpi=150, bbox_inches='tight')
        plt.close()
        
        log_experiment_milestone("Visualizations Complete", {'plot_saved': str(plot_path)})
    
    async def run_experiment(self) -> Dict[str, Any]:
        """Run the complete judge experiment."""
        log_experiment_milestone("Starting Multi-Judge Experiment")
        
        try:
            # Step 1: Load and prepare data
            data = self.load_and_prepare_data()
            
            # Step 2: Simulate personas if needed (for UltraFeedback)
            data_with_personas = await self.simulate_personas_if_needed(data)
            
            # Step 3: Initialize judges
            self.initialize_judges()
            
            # Step 4: Run judge inference
            data_with_judges = self.run_judge_inference(data_with_personas)
            
            # Step 5: Analyze correlations
            correlation_analysis = self.analyze_correlations(data_with_judges)
            
            # Step 6: Test aggregation models
            model_results = self.test_aggregation_models(data_with_judges)
            
            # Step 7: Create visualizations
            self.create_visualizations(correlation_analysis, model_results)
            
            # Step 8: Compile results
            experiment_results = {
                'config': self.config,
                'correlation_analysis': correlation_analysis,
                'model_results': model_results,
                'summary': {
                    'overall_correlation': correlation_analysis.get('overall_correlation', 0),
                    'best_model_r2': max([v['test_metrics']['r2'] for v in model_results.values() if 'test_metrics' in v], default=-1),
                    'normalization_helps': self._test_normalization_benefit(model_results),
                    'samples_processed': len(data_with_judges),
                    'run_name': self.run_name
                }
            }
            
            # Save final results
            results_path = self.run_dir / 'experiment_results.pkl'
            with open(results_path, 'wb') as f:
                pickle.dump(experiment_results, f)
            
            # Save summary JSON
            summary_path = self.run_dir / 'experiment_summary.json'
            with open(summary_path, 'w') as f:
                # Convert for JSON serialization
                summary = {}
                for key, value in experiment_results['summary'].items():
                    summary[key] = float(value) if isinstance(value, np.number) else value
                json.dump(summary, f, indent=2)
            
            log_experiment_complete({
                'overall_correlation': correlation_analysis.get('overall_correlation', 0),
                'best_model_r2': experiment_results['summary']['best_model_r2'],
                'normalization_helps': experiment_results['summary']['normalization_helps'],
                'samples_processed': len(data_with_judges),
                'api_calls_made': len(data_with_judges) * len(JUDGE_IDS),
                'run_name': self.run_name
            })
            
            return experiment_results
            
        except Exception as e:
            log_experiment_milestone(f"Experiment Failed: {e}")
            raise
    
    def _test_normalization_benefit(self, model_results: Dict[str, Any]) -> bool:
        """Test if normalization helps model performance."""
        raw_r2 = model_results.get('mlp_raw', {}).get('test_metrics', {}).get('r2', -1)
        norm_r2 = model_results.get('mlp_normalized', {}).get('test_metrics', {}).get('r2', -1)
        
        if raw_r2 == -1 or norm_r2 == -1:
            return False
        
        return norm_r2 > raw_r2 + 0.05  # Meaningful improvement threshold


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="Multi-Judge vs Persona Correlation Experiment")
    parser.add_argument('--data-source', choices=['ultrafeedback', 'personas'], default='personas',
                        help='Data source (default: personas)')
    parser.add_argument('--data-size', type=int, default=100,
                        help='Number of samples to use (default: 100)')
    parser.add_argument('--test-size', type=float, default=0.2,
                        help='Test set fraction (default: 0.2)')
    parser.add_argument('--concurrency', type=int, default=1,
                        help='Row-level API concurrency (default: 1, each row processes 5 judges in parallel)')
    parser.add_argument('--random-seed', type=int, default=42,
                        help='Random seed (default: 42)')
    parser.add_argument('--run-name', help='Custom run name (default: auto-generated)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Run with small dataset for testing')
    
    args = parser.parse_args()
    
    # Adjust for dry run
    if args.dry_run:
        args.data_size = min(args.data_size, 20)
        args.concurrency = 1  # Always 1 for dry run
        print("🧪 DRY RUN MODE: Using small dataset and conservative API usage")
    
    # Create experiment
    experiment = FullExperiment(
        data_source=args.data_source,
        data_size=args.data_size,
        test_size=args.test_size,
        random_seed=args.random_seed,
        concurrency=args.concurrency,
        run_name=args.run_name
    )
    
    try:
        results = await experiment.run_experiment()
        
        print("\n" + "="*80)
        print("🎯 MULTI-JUDGE EXPERIMENT COMPLETE!")
        print("="*80)
        
        # Print key findings
        overall_corr = results['summary']['overall_correlation']
        best_r2 = results['summary']['best_model_r2']
        norm_helps = results['summary']['normalization_helps']
        run_name = results['summary']['run_name']
        
        print(f"📊 KEY FINDINGS:")
        print(f"   Judge-Persona Correlation: {overall_corr:.3f}")
        print(f"   Best Model R²: {best_r2:.3f}")
        print(f"   Normalization Helps: {norm_helps}")
        
        print(f"\n📁 RESULTS:")
        print(f"   Run: {run_name}")
        print(f"   Directory: full_experiment_runs/{run_name}")
        print(f"   Data: full_experiment_runs/{run_name}/data/")
        print(f"   Results: full_experiment_runs/{run_name}/results/")
        print(f"   Plots: full_experiment_runs/{run_name}/plots/")
        print(f"   Logs: full_experiment_runs/{run_name}/logs/")
        
        # Interpretation
        if abs(overall_corr) > 0.5:
            print(f"\n✅ STRONG correlation found! Judges align well with human preferences.")
        elif abs(overall_corr) > 0.3:
            print(f"\n🟡 MODERATE correlation found. Judges partially align with human preferences.")
        else:
            print(f"\n❌ WEAK correlation found. Judges may not align well with human preferences.")
            print("   This could be a key research finding about judge-human misalignment!")
        
        print("="*80)
        
    except Exception as e:
        print(f"❌ Experiment failed: {e}")
        print("💡 Check API credentials and judge deployment status")
        raise


if __name__ == "__main__":
    asyncio.run(main())